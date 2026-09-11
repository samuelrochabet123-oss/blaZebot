# Blaze Bot — Render + Neon

## Arquivos

- `app.py` — dashboard Flask e inicialização do collector.
- `collector.py` — coleta em tempo real via Socket.IO e grava no Neon.
- `requirements.txt` — dependências.
- `.python-version` — Python 3.13.

## Render

Runtime: Python

Build Command:
`pip install -r requirements.txt`

Start Command:
`gunicorn --workers 1 --bind 0.0.0.0:$PORT app:app`

## Environment Variables

Obrigatória:
`DATABASE_URL` = connection string do Neon.

Opcionais:
- `RUN_COLLECTOR=true`
- `BLAZE_URL=https://api-gaming.blaze.bet.br`
- `SOCKET_PATH=/replication/`
- `BLAZE_ROOM=double_room_1`

## Importante

Esta versão NÃO usa o endpoint REST `/roulette_games/recent/1`.
A coleta é feita pelo Socket.IO/WebSocket em `/replication/`.

Não coloque senha do banco diretamente no código.
