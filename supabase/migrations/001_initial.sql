create extension if not exists pgcrypto;

create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  email text,
  created_at timestamptz not null default now()
);

create table if not exists public.conversions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  filename text not null,
  bank text not null default 'Banque non identifiée',
  transaction_count integer not null default 0,
  period_start date,
  period_end date,
  created_at timestamptz not null default now()
);

create table if not exists public.transactions (
  id uuid primary key default gen_random_uuid(),
  conversion_id uuid not null references public.conversions(id) on delete cascade,
  date date not null,
  type text not null check (type in ('CREDIT','DEBIT')),
  amount numeric(15,2) not null,
  name text,
  memo text,
  fitid text,
  created_at timestamptz not null default now()
);

alter table public.profiles enable row level security;
alter table public.conversions enable row level security;
alter table public.transactions enable row level security;

create policy "profiles own row" on public.profiles for all using (auth.uid() = id) with check (auth.uid() = id);
create policy "conversions own rows" on public.conversions for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "transactions through own conversion" on public.transactions for all using (
  exists (select 1 from public.conversions c where c.id = conversion_id and c.user_id = auth.uid())
) with check (
  exists (select 1 from public.conversions c where c.id = conversion_id and c.user_id = auth.uid())
);

create or replace function public.handle_new_user()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  insert into public.profiles (id,email) values (new.id,new.email) on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created after insert on auth.users for each row execute procedure public.handle_new_user();
