"""Public harness API (``/v1``): API-key authenticated, polling-first.

The routers here are mounted as a separate FastAPI application by
``api.v1.app.create_v1_app`` so that error bodies, request ids and rate-limit
headers follow the published contract without touching the internal
``/api/*`` routes. Field names are ``snake_case`` and ids carry public
prefixes (``ses_`` / ``msg_`` / ``prt_`` / ``qst_`` / ``fil_``); see
``api.v1.ids`` for the mapping to storage ids.
"""
