---
name: demo-echo
description: Demonstration skill for the durable skill job runtime. Echoes text immediately, after a simulated external wait, or after asking the user.
allowed-tools: skill_job
job-skill-keys: builtin:demo-echo
---

# Demo Echo

Use the generic `skill_job` tool with `skill: "builtin:demo-echo"`.

Operations:

- `echo` — input `{"text": "..."}`; completes immediately.
- `slow_echo` — input `{"text": "...", "delay_seconds": 5}`; the job waits in
  the background and completes on its own. Start it and end your turn — do
  not poll in a loop.
- `ask_then_echo` — the job asks the user a question and completes once they
  answer (through the job card, or via `skill_job` action `resume`).

After `start`, report the returned `job_id` and status to the user and stop.
The job card keeps updating without you.
