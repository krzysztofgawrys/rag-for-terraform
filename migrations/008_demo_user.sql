-- Demo user with readonly role for public demo access.
-- Password: demo (bcrypt hash below).
INSERT INTO users (email, display_name, password_hash, role)
VALUES (
    'demo@terraform-rag.io',
    'Demo User',
    '$2b$12$htDu2OVEsGDKYZbPUoQi7OGylBuZga7eoVxtKwc/d/KCCSXhgUHq.',
    'readonly'
)
ON CONFLICT (email) DO NOTHING;
