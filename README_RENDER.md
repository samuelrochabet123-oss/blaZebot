# Blaze Collector V2.2 — Render + Neon

## Arquivos

- `app.py` — dashboard Flask + inicialização do collector
- `collector.py` — Socket.IO collector
- `requirements.txt` — dependências
- `.python-version` — Python 3.13
- `RENDER_START_COMMAND.txt` — comando de inicialização do Render

## Environment Variables no Render

Obrigatória:

`DATABASE_URL` = sua connection string do Neon.

Opcionais:

- `RUN_COLLECTOR=true`
- `BLAZE_URL=https://api-gaming.blaze.bet.br`
- `SOCKET_PATH=/replication/`
- `BLAZE_ROOM=double_room_1`
- `COLLECTOR_STATUS_INTERVAL=30`
- `COLLECTOR_RETRY_DELAY=5`
- `MOSTRAR_TICKS=false`

## Start Command

`gunicorn --workers 1 --bind 0.0.0.0:$PORT app:app`

## Importante

O collector não usa a API REST `/roulette_games/recent/1`.
Ele mantém a arquitetura do V2.1 baseada em Socket.IO e no evento `double.tick`.

A senha do banco nunca deve ficar dentro do código.
