-- Demo seed data. Applied after schema.sql. Safe to re-run via `make db-reset`.
-- Rule: anything seeded here must be visibly marked in the UI as seed data (devices.platform = 'seed').

INSERT INTO authorities (id, name, city, email, escalation_email)
VALUES (
  '00000000-0000-4000-8000-000000000001',
  'Demo City Roads Department',
  'Demo City',
  'roads@example.org',
  'chief-engineer@example.org'
);

-- Replace the coordinates in db/seed_potholes.sql (optional, create it yourself) with real points on the demo route.
-- Do NOT seed fake "verified" potholes unless the demo script explicitly says so and the UI shows a Seed badge.
