# AI Task Bot

Create tasks from Telegram (text or voice) and manage them on a live web dashboard.

## What it does

- Text messages become tasks immediately.
- Voice messages get an instant “Transcribing…” reply, then a worker turns the audio into text.
- The dashboard shows every task in a board or a list, and updates as soon as something changes.
- Moving a task to Pending, In Progress, or Completed notifies the Telegram chat that created it.

## Architecture

- **Bot** talks to Telegram and calls the API.
- **API** (FastAPI) stores tasks and Telegram users in PostgreSQL.
- **Redis** carries two queues (`transcription_jobs`, `notification_jobs`) and a pub/sub channel (`task_events`) for the dashboard.
- **Worker** downloads voice notes, transcribes them, and sends Telegram updates.
- **Dashboard** (React) listens on Server-Sent Events at `/events`.

Speech-to-text uses an OpenAI-compatible endpoint. The sample env points at Groq Whisper.

## Run it

1. Copy the env file and fill in your own tokens:

```powershell
copy .env.example .env
```

`TELEGRAM_BOT_TOKEN` comes from BotFather. `STT_API_KEY` is a Groq or OpenAI speech-to-text key. The database values in the example are fine for local Docker.

2. Start the stack:

```powershell
docker compose up --build
```

3. Open the dashboard at http://localhost:3000. API docs are at http://localhost:8000/docs.

4. Message the bot. `/start` explains the commands, `/tasks` lists your latest tasks.

## Frontend only

Leave the API running (Docker publishes port 8000), then:

```powershell
cd frontend
npm install
npm run dev
```

Vite proxies `/api` to http://localhost:8000. Open http://localhost:5173.

## Status flow

New tasks start as Pending. A voice task stays in “Transcribing voice” until the worker finishes or reports a failure. Status buttons on the card write through `PATCH /tasks/{id}` and the bot confirms the change.
