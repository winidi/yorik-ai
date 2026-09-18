/**
 * The household, with each person's colour and photo, fetched once and
 * shared by every avatar in the app. Backend: GET /api/people/household.
 */
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

export interface Person {
  id: string;
  name: string;
  first_name: string;
  role: string;
  color: string;
  avatar_url: string | null;
}

let cache: Person[] | null = null;
let inflight: Promise<Person[]> | null = null;
const listeners = new Set<(p: Person[]) => void>();

export function loadPeople(force = false): Promise<Person[]> {
  if (cache && !force) return Promise.resolve(cache);
  if (inflight && !force) return inflight;
  inflight = api.get<{ people: Person[] }>("/api/people/household")
    .then(r => { cache = r.people; for (const fn of listeners) fn(cache); return cache; })
    .catch(() => cache || [])
    .finally(() => { inflight = null; });
  return inflight;
}

/** Call after the user changed their own colour or photo. */
export function invalidatePeople() { void loadPeople(true); }

export function usePeople(): Person[] {
  const [people, setPeople] = useState<Person[]>(cache || []);
  useEffect(() => {
    listeners.add(setPeople);
    void loadPeople();
    return () => { listeners.delete(setPeople); };
  }, []);
  return people;
}

/** Look up a member by id or by display name (the calendar only has names). */
export function usePerson(idOrName: string | null | undefined): Person | undefined {
  const people = usePeople();
  if (!idOrName) return undefined;
  const key = idOrName.trim().toLowerCase();
  return people.find(p => p.id === idOrName) || people.find(p => p.name.trim().toLowerCase() === key)
    || people.find(p => p.first_name && p.first_name.trim().toLowerCase() === key);
}
