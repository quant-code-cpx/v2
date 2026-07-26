-- PostgreSQL does not permit a newly added enum value to be used until this transaction
-- commits. Keep enum expansion isolated from the following account-access migration.
ALTER TYPE "Role" ADD VALUE IF NOT EXISTS 'SUPER_ADMIN';
ALTER TYPE "UserStatus" ADD VALUE IF NOT EXISTS 'DELETED';
