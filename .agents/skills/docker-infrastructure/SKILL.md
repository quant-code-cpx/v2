---
name: docker-infrastructure
description: Design, create, review, or revise Dockerfiles, Compose configuration, container images, local containerized dependencies, health checks, volumes, and environment-specific container deployment for this repository. Use for any Docker, Docker Compose, containerization, image build, container runtime, or container-based local development task.
---

# Docker Infrastructure

Keep all three services independently buildable, testable, runnable, and deployable.

## Workflow

1. Read root guidance plus README and configuration in every affected service.
2. Confirm affected service technology stack and runtime requirements are decided. If not, document required decisions instead of guessing or initializing a stack.
3. Use root Compose configuration to coordinate local services and shared dependencies.
4. Give every long-running service a meaningful health check.
5. Pin external image versions and use named volumes for stateful local data.
6. Optimize Dockerfiles for cache reuse and run final images as non-root users.
7. Inject configuration through environment variables. Never bake secrets, tokens, real accounts, or production configuration into images or Compose files.
8. Express development, test, and production differences through explicit configuration or overrides; avoid copied Compose files that can drift.
9. Update `.env.example`, affected service README files, and architecture or deployment documentation.
10. Validate Compose rendering, affected image builds, container startup, health checks, and shutdown behavior. Report unrun checks and reasons.

## Review Checklist

- Service boundaries remain explicit; no source imports cross service directories.
- Build contexts do not send secrets, local data, caches, or large market datasets.
- Images use deterministic dependency installation and small runtime stages where practical.
- Services handle termination signals and do not rely on manual initialization.
- Stateful migrations are automated, reversible, and separated from application startup when risk requires.
- Local configuration does not imply production topology or weaken production security.
