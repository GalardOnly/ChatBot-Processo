-- =========================================================================
-- Schema da Biblioteca de Jurisprudencia (pessoal por usuario + global)
-- Rode este SQL no editor do Supabase (SQL Editor -> New query -> Run).
-- Idempotente: pode rodar mais de uma vez sem quebrar.
-- =========================================================================

create extension if not exists vector;
create extension if not exists pgcrypto;

-- =========================================================================
-- Tabelas
-- =========================================================================

-- Cada peca de jurisprudencia (acordao, sumula, REsp, HC, etc.)
-- user_id NULL = biblioteca global (curada manualmente via SQL)
create table if not exists jurisprudence (
  id             uuid primary key default gen_random_uuid(),
  user_id        uuid references auth.users(id) on delete cascade,
  title          text not null,
  court          text,                       -- "STF" | "STJ" | "TJSP" | ...
  case_number    text,                       -- "HC 126.292/SP"
  rapporteur     text,                       -- relator
  judgment_date  date,
  tags           text[] default '{}'::text[],
  source_url     text,
  full_text      text not null,
  total_chunks   int not null default 0,
  created_at     timestamptz not null default now()
);

-- Chunks com embeddings (analogo a 'chunks' do processo)
create table if not exists jurisprudence_chunks (
  id                bigserial primary key,
  jurisprudence_id  uuid not null references jurisprudence(id) on delete cascade,
  chunk_index       int  not null,
  word_count        int  default 0,
  text              text not null,
  embedding         vector(1024) not null
);

-- =========================================================================
-- Indices
-- =========================================================================

create index if not exists jurisprudence_user_idx
  on jurisprudence (user_id, created_at desc);

create index if not exists jurisprudence_chunks_juris_idx
  on jurisprudence_chunks (jurisprudence_id);

create index if not exists jurisprudence_chunks_embedding_idx
  on jurisprudence_chunks
  using hnsw (embedding vector_cosine_ops)
  with (m = 16, ef_construction = 64);

-- =========================================================================
-- Row Level Security
-- =========================================================================

alter table jurisprudence enable row level security;
alter table jurisprudence_chunks enable row level security;

drop policy if exists jurisprudence_select on jurisprudence;
drop policy if exists jurisprudence_insert on jurisprudence;
drop policy if exists jurisprudence_delete on jurisprudence;

drop policy if exists jurisprudence_chunks_select on jurisprudence_chunks;
drop policy if exists jurisprudence_chunks_insert on jurisprudence_chunks;
drop policy if exists jurisprudence_chunks_delete on jurisprudence_chunks;

-- Pecas: usuario ve as proprias + as globais (user_id is null)
-- mas so insere/deleta as proprias
create policy jurisprudence_select on jurisprudence
  for select using (user_id = auth.uid() or user_id is null);
create policy jurisprudence_insert on jurisprudence
  for insert with check (user_id = auth.uid());
create policy jurisprudence_delete on jurisprudence
  for delete using (user_id = auth.uid());

-- Chunks: acessiveis se a peca pai for visivel ao usuario
create policy jurisprudence_chunks_select on jurisprudence_chunks
  for select using (
    exists (
      select 1 from jurisprudence j
      where j.id = jurisprudence_chunks.jurisprudence_id
        and (j.user_id = auth.uid() or j.user_id is null)
    )
  );
create policy jurisprudence_chunks_insert on jurisprudence_chunks
  for insert with check (
    exists (
      select 1 from jurisprudence j
      where j.id = jurisprudence_chunks.jurisprudence_id
        and j.user_id = auth.uid()
    )
  );
create policy jurisprudence_chunks_delete on jurisprudence_chunks
  for delete using (
    exists (
      select 1 from jurisprudence j
      where j.id = jurisprudence_chunks.jurisprudence_id
        and j.user_id = auth.uid()
    )
  );

-- =========================================================================
-- Funcao RPC: busca por similaridade
-- Retorna chunks da biblioteca pessoal (auth.uid()) + global (user_id IS NULL)
-- =========================================================================

create or replace function match_jurisprudence(
  query_embedding vector(1024),
  match_count     int default 5
)
returns table (
  chunk_id          bigint,
  jurisprudence_id  uuid,
  chunk_index       int,
  text              text,
  similarity        float,
  title             text,
  court             text,
  case_number       text,
  rapporteur        text,
  judgment_date     date,
  is_global         boolean
)
language sql
stable
security invoker
as $$
  select
    jc.id          as chunk_id,
    jc.jurisprudence_id,
    jc.chunk_index,
    jc.text,
    1 - (jc.embedding <=> query_embedding) as similarity,
    j.title, j.court, j.case_number, j.rapporteur, j.judgment_date,
    (j.user_id is null) as is_global
  from jurisprudence_chunks jc
  inner join jurisprudence j on j.id = jc.jurisprudence_id
  where j.user_id = auth.uid() or j.user_id is null
  order by jc.embedding <=> query_embedding
  limit match_count
$$;
