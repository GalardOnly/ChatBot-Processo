-- =========================================================================
-- LGPD - Adicoes ao schema do Defensor IA
-- Lei 13.709/2018 - Lei Geral de Protecao de Dados
--
-- Execute este arquivo APOS o supabase_schema.sql original.
-- Todos os comandos sao idempotentes (IF NOT EXISTS / OR REPLACE).
-- =========================================================================


-- =========================================================================
-- 1. COLUNA DE EXPIRACAO em processes (retencao de dados - LGPD art. 16)
-- Nota: coluna gerada nao funciona com interval no Supabase/PG14+.
-- Usamos trigger BEFORE INSERT para preencher automaticamente.
-- =========================================================================

-- Adiciona a coluna como campo normal
alter table processes
  add column if not exists expires_at timestamptz;

-- Preenche linhas existentes que ainda nao tem expires_at
update processes
set    expires_at = created_at + interval '730 days'
where  expires_at is null;

-- Funcao que preenche expires_at automaticamente no insert
create or replace function _set_process_expires_at()
returns trigger
language plpgsql
as $$
begin
  new.expires_at := new.created_at + interval '730 days';
  return new;
end;
$$;

-- Trigger disparado antes de cada INSERT em processes
drop trigger if exists trg_process_expires_at on processes;
create trigger trg_process_expires_at
  before insert on processes
  for each row
  execute function _set_process_expires_at();

-- Indice para facilitar limpeza automatica
create index if not exists processes_expires_idx on processes (expires_at);


-- =========================================================================
-- 2. REGISTRO DE CONSENTIMENTO (LGPD art. 7, I e art. 8)
-- Armazena o aceite do Defensor Publico ao Termo de Consentimento.
-- =========================================================================

create table if not exists lgpd_consents (
  id           bigserial primary key,
  user_id      uuid not null references auth.users(id) on delete cascade,
  accepted_at  timestamptz not null default now(),
  term_version text not null default '1.0',  -- versao do termo aceito
  ip_hint      text,                          -- primeiros octetos do IP (sem identificar exatamente)
  user_agent   text
);

create index if not exists lgpd_consents_user_idx on lgpd_consents (user_id, accepted_at desc);

alter table lgpd_consents enable row level security;

drop policy if exists lgpd_consents_select on lgpd_consents;
drop policy if exists lgpd_consents_insert on lgpd_consents;

create policy lgpd_consents_select on lgpd_consents
  for select using (auth.uid() = user_id);

create policy lgpd_consents_insert on lgpd_consents
  for insert with check (auth.uid() = user_id);


-- =========================================================================
-- 3. LOG DE ACESSO A DADOS (LGPD art. 37 - registro de operacoes)
-- Registra quem acessou qual processo e quando.
-- =========================================================================

create table if not exists data_access_log (
  id           bigserial primary key,
  user_id      uuid not null references auth.users(id) on delete cascade,
  process_id   uuid references processes(id) on delete set null,
  action       text not null,  -- 'upload', 'chat', 'action_summary', 'action_probatoria',
                                --  'action_prescricao', 'action_audiencia', 'export', 'delete'
  occurred_at  timestamptz not null default now()
);

create index if not exists data_access_log_user_idx    on data_access_log (user_id, occurred_at desc);
create index if not exists data_access_log_process_idx on data_access_log (process_id);

alter table data_access_log enable row level security;

drop policy if exists data_access_log_select on data_access_log;
drop policy if exists data_access_log_insert on data_access_log;

-- Usuario ve apenas seus proprios logs
create policy data_access_log_select on data_access_log
  for select using (auth.uid() = user_id);

-- Usuario so pode inserir logs para si mesmo
create policy data_access_log_insert on data_access_log
  for insert with check (auth.uid() = user_id);


-- =========================================================================
-- 4. PEDIDOS DE ELIMINACAO DE DADOS (LGPD art. 18, VI)
-- Registra e rastreia pedidos de exclusao de conta/dados.
-- =========================================================================

create table if not exists deletion_requests (
  id              bigserial primary key,
  user_id         uuid not null references auth.users(id) on delete cascade,
  requested_at    timestamptz not null default now(),
  reason          text,           -- motivo informado pelo usuario (opcional)
  status          text not null default 'pending'  -- 'pending', 'completed', 'cancelled'
                  check (status in ('pending', 'completed', 'cancelled')),
  completed_at    timestamptz
);

create index if not exists deletion_requests_user_idx on deletion_requests (user_id);

alter table deletion_requests enable row level security;

drop policy if exists deletion_requests_select on deletion_requests;
drop policy if exists deletion_requests_insert on deletion_requests;

create policy deletion_requests_select on deletion_requests
  for select using (auth.uid() = user_id);

create policy deletion_requests_insert on deletion_requests
  for insert with check (auth.uid() = user_id);


-- =========================================================================
-- 5. FUNCAO: EXPORTAR DADOS DO USUARIO (LGPD art. 18, I e II)
-- Retorna um JSON com todos os dados tratados do usuario.
-- =========================================================================

create or replace function export_user_data(p_user_id uuid)
returns jsonb
language plpgsql
security invoker
as $$
declare
  result jsonb;
begin
  -- Garante que o usuario so exporta seus proprios dados
  if auth.uid() != p_user_id then
    raise exception 'Acesso negado: voce so pode exportar seus proprios dados.';
  end if;

  select jsonb_build_object(
    'exported_at',   now(),
    'user_id',       p_user_id,
    'lgpd_consents', (
      select coalesce(jsonb_agg(jsonb_build_object(
        'accepted_at',  accepted_at,
        'term_version', term_version
      )), '[]'::jsonb)
      from lgpd_consents
      where user_id = p_user_id
    ),
    'processes', (
      select coalesce(jsonb_agg(jsonb_build_object(
        'id',           p.id,
        'filename',     p.filename,
        'total_pages',  p.total_pages,
        'total_chunks', p.total_chunks,
        'created_at',   p.created_at,
        'expires_at',   p.expires_at,
        'messages', (
          select coalesce(jsonb_agg(jsonb_build_object(
            'role',       m.role,
            'content',    m.content,
            'created_at', m.created_at
          ) order by m.created_at), '[]'::jsonb)
          from messages m
          where m.process_id = p.id
        )
      )), '[]'::jsonb)
      from processes p
      where p.user_id = p_user_id
    ),
    'access_log', (
      select coalesce(jsonb_agg(jsonb_build_object(
        'action',      action,
        'process_id',  process_id,
        'occurred_at', occurred_at
      ) order by occurred_at desc), '[]'::jsonb)
      from data_access_log
      where user_id = p_user_id
      limit 500
    )
  ) into result;

  -- Registra o proprio ato de exportacao no log
  insert into data_access_log (user_id, action)
  values (p_user_id, 'export');

  return result;
end;
$$;


-- =========================================================================
-- 6. FUNCAO: ELIMINAR TODOS OS DADOS DO USUARIO (LGPD art. 18, VI)
-- Apaga processos, chunks, mensagens e logs. Nao apaga a conta auth.users
-- (isso e feito via Supabase Auth admin ou pelo proprio usuario em Settings).
-- =========================================================================

create or replace function delete_user_data(p_user_id uuid)
returns void
language plpgsql
security invoker
as $$
begin
  if auth.uid() != p_user_id then
    raise exception 'Acesso negado: voce so pode excluir seus proprios dados.';
  end if;

  -- processes -> ON DELETE CASCADE elimina chunks e messages automaticamente
  delete from processes      where user_id = p_user_id;
  delete from data_access_log where user_id = p_user_id;
  delete from lgpd_consents   where user_id = p_user_id;

  -- Marca pedido de exclusao como concluido (se existir)
  update deletion_requests
  set    status = 'completed', completed_at = now()
  where  user_id = p_user_id and status = 'pending';

  -- Insere um registro final (fica no log ate a conta ser excluida)
  insert into deletion_requests (user_id, status, reason, completed_at)
  values (p_user_id, 'completed', 'Exclusao solicitada e executada pelo proprio usuario', now())
  on conflict do nothing;

end;
$$;


-- =========================================================================
-- 7. FUNCAO: LIMPAR PROCESSOS EXPIRADOS (rodar periodicamente via pg_cron
--    ou manualmente - LGPD art. 16, retencao minima necessaria)
-- =========================================================================

create or replace function cleanup_expired_processes()
returns int
language plpgsql
security definer
as $$
declare
  deleted_count int;
begin
  delete from processes
  where expires_at < now();

  get diagnostics deleted_count = row_count;
  return deleted_count;
end;
$$;

-- Comentario: Para execucao automatica, configure no Supabase Dashboard:
-- Database -> Extensions -> pg_cron, depois:
-- select cron.schedule('cleanup-expired', '0 3 * * *', 'select cleanup_expired_processes()');


-- =========================================================================
-- FIM DO SCRIPT LGPD
-- =========================================================================
