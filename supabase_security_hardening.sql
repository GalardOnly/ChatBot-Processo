-- =========================================================================
-- HARDENING DE SEGURANCA - Auditoria
-- Aplicar APOS supabase_schema.sql, supabase_lgpd.sql, supabase_feedback.sql,
-- supabase_jurisprudence.sql. Idempotente.
--
-- Cobre:
--   P1.5 - bloquear insercao de role='assistant' pelo cliente
--   P1.6 - rate limit no banco por user_id
--   P2.9 - whitelist de dominio / aprovacao admin (tabela user_status)
-- =========================================================================


-- =========================================================================
-- 1. MESSAGES: cliente so pode inserir role='user' e do proprio processo
-- =========================================================================
-- A policy antiga so checava auth.uid()=user_id, deixando o cliente forjar
-- mensagens de role='assistant' diretamente via PostgREST.
drop policy if exists messages_insert on messages;
create policy messages_insert on messages
  for insert with check (
    auth.uid() = user_id
    and role = 'user'
    and exists (
      select 1 from processes p
      where p.id = messages.process_id and p.user_id = auth.uid()
    )
  );


-- RPC SECURITY DEFINER: unica via para inserir role='assistant'
create or replace function save_assistant_message(
  p_process_id uuid,
  p_content    text,
  p_sources    jsonb default null,
  p_action_key text default null
)
returns bigint
language plpgsql
security definer
set search_path = public
as $$
declare
  new_id bigint;
  caller uuid;
begin
  caller := auth.uid();
  if caller is null then
    raise exception 'Acesso negado: usuario nao autenticado';
  end if;

  -- Verifica que o processo pertence ao chamador (defesa em profundidade
  -- alem do RLS, ja que a funcao roda como definer).
  if not exists (
    select 1 from processes where id = p_process_id and user_id = caller
  ) then
    raise exception 'Acesso negado: processo nao pertence ao usuario';
  end if;

  insert into messages (user_id, process_id, role, content, sources, action_key)
  values (caller, p_process_id, 'assistant', p_content, p_sources, p_action_key)
  returning id into new_id;

  return new_id;
end;
$$;

-- Permite que role autenticado chame; service_role tambem.
grant execute on function save_assistant_message(uuid, text, jsonb, text)
  to authenticated, service_role;


-- =========================================================================
-- 2. RATE LIMIT NO BANCO (Audit M4)
-- Tabela leve so com user_id, action e timestamp. Indice por (user_id,action,ts)
-- =========================================================================
create table if not exists rate_limit_log (
  id          bigserial primary key,
  user_id     uuid not null references auth.users(id) on delete cascade,
  action      text not null,
  occurred_at timestamptz not null default now()
);

create index if not exists rate_limit_log_idx
  on rate_limit_log (user_id, action, occurred_at desc);

alter table rate_limit_log enable row level security;

-- O usuario nao precisa ler/escrever direto; o RPC abaixo faz tudo
-- com security definer. Mantemos uma policy permissiva de SELECT proprio
-- por transparencia (auditoria local).
drop policy if exists rate_limit_log_select on rate_limit_log;
create policy rate_limit_log_select on rate_limit_log
  for select using (auth.uid() = user_id);


-- RPC atomica: conta janela + insere registro. Retorna json:
--   {allowed: bool, count: int, max: int, retry_after_s: int}
create or replace function check_and_record_rate_limit(
  p_action     text,
  p_max_calls  int,
  p_window_s   int
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  caller     uuid := auth.uid();
  current_n  int;
  oldest_ts  timestamptz;
  retry      int;
begin
  if caller is null then
    raise exception 'Acesso negado: usuario nao autenticado';
  end if;

  -- Conta chamadas dentro da janela
  select count(*), min(occurred_at)
    into current_n, oldest_ts
    from rate_limit_log
    where user_id = caller
      and action  = p_action
      and occurred_at > now() - make_interval(secs => p_window_s);

  if current_n >= p_max_calls then
    retry := greatest(
      1,
      p_window_s - extract(epoch from (now() - oldest_ts))::int
    );
    return jsonb_build_object(
      'allowed', false,
      'count',   current_n,
      'max',     p_max_calls,
      'retry_after_s', retry
    );
  end if;

  -- Permite e registra
  insert into rate_limit_log (user_id, action) values (caller, p_action);
  return jsonb_build_object(
    'allowed', true,
    'count',   current_n + 1,
    'max',     p_max_calls,
    'retry_after_s', 0
  );
end;
$$;

grant execute on function check_and_record_rate_limit(text, int, int)
  to authenticated;


-- =========================================================================
-- 3. USER STATUS (P2.9: aprovacao manual / whitelist de dominio)
-- =========================================================================
create table if not exists user_status (
  user_id            uuid primary key references auth.users(id) on delete cascade,
  status             text not null default 'pending'
                     check (status in ('pending', 'approved', 'rejected')),
  email_domain       text,
  approved_by_admin  boolean not null default false,
  approved_at        timestamptz,
  notes              text,
  created_at         timestamptz not null default now()
);

create index if not exists user_status_status_idx on user_status (status);

alter table user_status enable row level security;

drop policy if exists user_status_select on user_status;
drop policy if exists user_status_insert on user_status;

-- Usuario ve o proprio status (precisa pra UI dizer "aguardando aprovacao")
create policy user_status_select on user_status
  for select using (auth.uid() = user_id);

-- Usuario pode auto-inserir registro inicial (sempre como pending)
create policy user_status_insert on user_status
  for insert with check (auth.uid() = user_id and status = 'pending');

-- Mudanca para 'approved' deve ser feita pelo service_role / admin manualmente
-- via Dashboard ou RPC privada (nao incluida aqui pra evitar escalada).


-- RPC para o app verificar/registrar status do usuario logado.
-- Retorna 'approved' | 'pending' | 'rejected'.
create or replace function get_or_create_user_status(
  p_allowed_domains text[] default null
)
returns text
language plpgsql
security definer
set search_path = public
as $$
declare
  caller       uuid := auth.uid();
  current_st   text;
  caller_email text;
  caller_domain text;
  is_whitelisted boolean := false;
begin
  if caller is null then
    raise exception 'Acesso negado: usuario nao autenticado';
  end if;

  -- Status existente?
  select status into current_st from user_status where user_id = caller;
  if found then
    return current_st;
  end if;

  -- Primeira vez: extrai dominio e verifica whitelist
  select email into caller_email from auth.users where id = caller;
  if caller_email is null then
    return 'pending';
  end if;

  caller_domain := lower(split_part(caller_email, '@', 2));

  if p_allowed_domains is not null and array_length(p_allowed_domains, 1) > 0 then
    is_whitelisted := caller_domain = any(
      array(select lower(d) from unnest(p_allowed_domains) as d)
    );
  end if;

  insert into user_status (user_id, status, email_domain,
                           approved_by_admin, approved_at)
  values (
    caller,
    case when is_whitelisted then 'approved' else 'pending' end,
    caller_domain,
    is_whitelisted,
    case when is_whitelisted then now() else null end
  );

  return case when is_whitelisted then 'approved' else 'pending' end;
end;
$$;

grant execute on function get_or_create_user_status(text[]) to authenticated;


-- =========================================================================
-- FIM DO HARDENING
-- =========================================================================
