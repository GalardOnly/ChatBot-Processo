-- =========================================================================
-- Schema do Assistente Juridico para Defensoria
-- Rode este SQL no editor do Supabase (SQL Editor -> New query -> Run).
-- =========================================================================

-- Extensao pgvector (embeddings de 1024 dim do voyage-law-2)
create extension if not exists vector;

-- =========================================================================
-- Tabelas
-- =========================================================================

create table if not exists processes (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null references auth.users(id) on delete cascade,
  filename     text not null,
  total_pages  int  not null default 0,
  total_chunks int  not null default 0,
  created_at   timestamptz not null default now()
);

create table if not exists chunks (
  id           bigserial primary key,
  process_id   uuid not null references processes(id) on delete cascade,
  chunk_index  int  not null,
  page_num     int  not null,
  word_count   int  not null default 0,
  text         text not null,
  embedding    vector(1024) not null
);

create table if not exists messages (
  id          bigserial primary key,
  process_id  uuid not null references processes(id) on delete cascade,
  user_id     uuid not null references auth.users(id) on delete cascade,
  role        text not null check (role in ('user','assistant')),
  content     text not null,
  sources     jsonb,
  created_at  timestamptz not null default now()
);

-- =========================================================================
-- Indices
-- =========================================================================

create index if not exists processes_user_idx   on processes (user_id, created_at desc);
create index if not exists chunks_process_idx   on chunks   (process_id);
create index if not exists messages_process_idx on messages (process_id, created_at);

-- HNSW para busca por similaridade cosseno
create index if not exists chunks_embedding_idx
  on chunks
  using hnsw (embedding vector_cosine_ops)
  with (m = 16, ef_construction = 64);

-- =========================================================================
-- Row Level Security
-- =========================================================================

alter table processes enable row level security;
alter table chunks    enable row level security;
alter table messages  enable row level security;

-- Drop antigas (idempotente)
drop policy if exists processes_select on processes;
drop policy if exists processes_insert on processes;
drop policy if exists processes_delete on processes;
drop policy if exists chunks_select    on chunks;
drop policy if exists chunks_insert    on chunks;
drop policy if exists chunks_delete    on chunks;
drop policy if exists messages_select  on messages;
drop policy if exists messages_insert  on messages;

-- Processos: cada usuario so ve / mexe nos seus
create policy processes_select on processes
  for select using (auth.uid() = user_id);
create policy processes_insert on processes
  for insert with check (auth.uid() = user_id);
create policy processes_delete on processes
  for delete using (auth.uid() = user_id);

-- Chunks: acessiveis se o processo pai for do usuario
create policy chunks_select on chunks
  for select using (
    exists (select 1 from processes p
            where p.id = chunks.process_id and p.user_id = auth.uid())
  );
create policy chunks_insert on chunks
  for insert with check (
    exists (select 1 from processes p
            where p.id = chunks.process_id and p.user_id = auth.uid())
  );
create policy chunks_delete on chunks
  for delete using (
    exists (select 1 from processes p
            where p.id = chunks.process_id and p.user_id = auth.uid())
  );

-- Mensagens
create policy messages_select on messages
  for select using (auth.uid() = user_id);
create policy messages_insert on messages
  for insert with check (auth.uid() = user_id);

-- =========================================================================
-- Funcao RPC de busca vetorial
-- =========================================================================

create or replace function match_chunks(
  query_embedding   vector(1024),
  match_process_id  uuid,
  match_count       int default 6
)
returns table (
  id           bigint,
  process_id   uuid,
  chunk_index  int,
  page_num     int,
  text         text,
  similarity   float
)
language sql
stable
security invoker
as $$
  select c.id, c.process_id, c.chunk_index, c.page_num, c.text,
         1 - (c.embedding <=> query_embedding) as similarity
  from chunks c
  where c.process_id = match_process_id
  order by c.embedding <=> query_embedding
  limit match_count
$$;
