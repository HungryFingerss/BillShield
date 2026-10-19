-- No login: a user is identified by email. Only the backend (secret key) touches these tables and files;
-- RLS is on with no policies, so the public keys can't read or write anything.
create table if not exists public.users (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  email text not null unique check (email = lower(email)),
  created_at timestamptz not null default now()
);

-- One row per bill check: what was read, what code found, what the user disputed, and the LLM's message.
create table if not exists public.checks (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users(id) on delete cascade,
  bill_path text not null,
  promised_path text,
  promised_text text,
  bill jsonb not null,
  promised jsonb,
  findings jsonb not null,
  disputed jsonb,
  explanation text,
  message text,
  status text not null default 'needs_review' check (status in ('needs_review', 'done')),
  created_at timestamptz not null default now()
);

create index if not exists checks_user_id_idx on public.checks (user_id);

alter table public.users enable row level security;
alter table public.checks enable row level security;

-- Official GST goods rates by HSN, loaded from CBIC notifications by tools/load_hsn_rates.py. Standalone reference table.
create table if not exists public.hsn_rates (
  id bigint generated always as identity primary key,
  hsn text not null check (hsn ~ '^[0-9]{2,8}$'),
  excludes text[] not null default '{}',
  hsn_entry text not null,
  description text not null,
  gst_rate numeric(5, 2) not null,
  notification text not null,
  schedule text not null,
  serial text not null,
  effective_from date not null
);

create index if not exists hsn_rates_hsn_idx on public.hsn_rates (hsn);

alter table public.hsn_rates
  add column if not exists search tsvector generated always as (to_tsvector('english', description)) stored;

create index if not exists hsn_rates_search_idx on public.hsn_rates using gin (search);

-- Official entries whose description matches any of the words, best matches first.
create or replace function public.search_hsn_rates(words text[], max_rows int default 15)
returns setof public.hsn_rates
language sql stable
set search_path = ''
as $$
  select r.*
  from public.hsn_rates r,
       to_tsquery('english', array_to_string(array(select quote_literal(w) from unnest(words) w), ' | ')) q
  where r.search @@ q
  order by ts_rank(r.search, q) desc, r.id
  limit max_rows;
$$;

revoke execute on function public.search_hsn_rates(text[], int) from public, anon, authenticated;

alter table public.hsn_rates enable row level security;

-- Files live under <user_id>/<check_id>/.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('bill-documents', 'bill-documents', false, 20971520, array['application/pdf', 'image/jpeg', 'image/png', 'image/webp'])
on conflict (id) do nothing;
