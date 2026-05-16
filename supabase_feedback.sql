-- =========================================================================
-- Schema de Feedback (avaliacao das respostas do assistente)
-- Rode no SQL Editor do Supabase. Idempotente.
-- =========================================================================

-- Tabela: 1 voto por (mensagem, usuario)
create table if not exists message_feedback (
  id           bigserial primary key,
  message_id   bigint not null references messages(id) on delete cascade,
  user_id      uuid   not null references auth.users(id) on delete cascade,
  rating       text   not null check (rating in ('positive', 'negative')),
  comment      text,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  unique (message_id, user_id)
);

create index if not exists message_feedback_message_idx on message_feedback(message_id);
create index if not exists message_feedback_user_idx    on message_feedback(user_id);
create index if not exists message_feedback_rating_idx  on message_feedback(rating);

-- Trigger para manter updated_at automaticamente
create or replace function set_message_feedback_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

drop trigger if exists message_feedback_updated_at on message_feedback;
create trigger message_feedback_updated_at
  before update on message_feedback
  for each row execute function set_message_feedback_updated_at();

-- =========================================================================
-- Row Level Security
-- =========================================================================

alter table message_feedback enable row level security;

drop policy if exists message_feedback_select on message_feedback;
drop policy if exists message_feedback_insert on message_feedback;
drop policy if exists message_feedback_update on message_feedback;
drop policy if exists message_feedback_delete on message_feedback;

create policy message_feedback_select on message_feedback
  for select using (user_id = auth.uid());
create policy message_feedback_insert on message_feedback
  for insert with check (user_id = auth.uid());
create policy message_feedback_update on message_feedback
  for update using (user_id = auth.uid()) with check (user_id = auth.uid());
create policy message_feedback_delete on message_feedback
  for delete using (user_id = auth.uid());

-- =========================================================================
-- Coluna action_key na tabela messages (preparacao para few-shot futuro)
-- Nao quebra schema existente: nullable e indexada parcial
-- =========================================================================

alter table messages add column if not exists action_key text;
create index if not exists messages_action_idx
  on messages(action_key)
  where action_key is not null;
