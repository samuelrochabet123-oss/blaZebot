import os
from datetime import datetime

import psycopg2

APOSTA_BASE = 1.00

CORES = {
    0: "BRANCO",
    1: "VERMELHO",
    2: "PRETO",
}

# Convenção única do motor: R=vermelho, P=preto, W=branco.
COR_SIGLA = {
    "BRANCO": "W",
    "VERMELHO": "R",
    "PRETO": "P",
}


def get_db_connection():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("❌ MOTOR: DATABASE_URL não configurada.")
        return None

    try:
        if "sslmode=" not in database_url:
            separator = "&" if "?" in database_url else "?"
            database_url = f"{database_url}{separator}sslmode=require"
        return psycopg2.connect(database_url, connect_timeout=15)
    except Exception as e:
        print(f"❌ MOTOR: erro PostgreSQL: {e}")
        return None


def init_engine_db():
    conn = get_db_connection()
    if not conn:
        return False

    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_estado (
                    id INTEGER PRIMARY KEY,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    whites INTEGER DEFAULT 0,
                    profit NUMERIC(12,2) DEFAULT 0.00,
                    sinal_ativo VARCHAR(150),
                    cor_sinal VARCHAR(5),
                    ultima_rodada_processada VARCHAR(100),
                    rodada_base_sinal VARCHAR(100),
                    motor_ativo BOOLEAN DEFAULT TRUE,
                    ultima_estrategia VARCHAR(150),
                    atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            columns = [
                ("wins", "INTEGER DEFAULT 0"),
                ("losses", "INTEGER DEFAULT 0"),
                ("whites", "INTEGER DEFAULT 0"),
                ("profit", "NUMERIC(12,2) DEFAULT 0.00"),
                ("sinal_ativo", "VARCHAR(150)"),
                ("cor_sinal", "VARCHAR(5)"),
                ("ultima_rodada_processada", "VARCHAR(100)"),
                ("rodada_base_sinal", "VARCHAR(100)"),
                ("motor_ativo", "BOOLEAN DEFAULT TRUE"),
                ("ultima_estrategia", "VARCHAR(150)"),
                ("atualizado_em", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
            ]

            for name, definition in columns:
                cur.execute(
                    f"ALTER TABLE bot_estado "
                    f"ADD COLUMN IF NOT EXISTS {name} {definition};"
                )

            cur.execute("""
                INSERT INTO bot_estado
                    (id, wins, losses, whites, profit, motor_ativo)
                VALUES (1, 0, 0, 0, 0.00, TRUE)
                ON CONFLICT (id) DO NOTHING;
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS estrategia_sinais (
                    id SERIAL PRIMARY KEY,
                    rodada_base VARCHAR(100) NOT NULL,
                    estrategia VARCHAR(150) NOT NULL,
                    cor_prevista VARCHAR(5) NOT NULL,
                    rodada_resultado VARCHAR(100),
                    cor_resultado VARCHAR(5),
                    resultado VARCHAR(20),
                    criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    resolvido_em TIMESTAMP
                );
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_estrategia_sinais_base
                ON estrategia_sinais(rodada_base);
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_estrategia_sinais_estrategia
                ON estrategia_sinais(estrategia);
            """)

        conn.commit()
        conn.close()
        return True

    except Exception as e:
        print(f"❌ MOTOR: erro inicializando banco: {e}")
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return False


def get_active_signal(rolls, colors):
    """
    Motor NOVO. Não gera EST 1, EST 2, EST 3, EST 4 ou EST 5.

    Estratégias ativas:
      1) VI → VI → R
      2) VI → PP → P
      3) VI → VI → VI → R
      4) ⚪ WHITE + 13 → R

    Convenção:
      R = vermelho
      P = preto
      W = branco

    WHITE + 13:
      W imediatamente seguido por uma rodada com roll 13
      gera previsão R para a próxima rodada.
    """
    rolls = list(rolls)
    colors = list(colors)
    n = len(colors)

    if n < 2:
        return None

    # 1) Dois vermelhos consecutivos -> vermelho.
    if colors[-2:] == ["R", "R"]:
        return ("VI → VI → R", "R")

    # 2) Vermelho + dois pretos -> preto.
    if n >= 3 and colors[-3:] == ["R", "P", "P"]:
        return ("VI → PP → P", "P")

    # 3) Três vermelhos consecutivos -> vermelho.
    if n >= 3 and colors[-3:] == ["R", "R", "R"]:
        return ("VI → VI → VI → R", "R")

    # 4) Branco + rodada com roll 13 -> vermelho.
    if colors[-2] == "W" and rolls[-1] == 13:
        return ("⚪ WHITE + 13 → R", "R")

    return None


def carregar_historico(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT rodada_id, color, roll
            FROM blaze_historico
            WHERE status = 'complete'
              AND color IN (0, 1, 2)
              AND roll IS NOT NULL
            ORDER BY id ASC;
        """)
        rows = cur.fetchall()

    rodadas, rolls, colors = [], [], []

    for rodada_id, color, roll in rows:
        try:
            color = int(color)
            roll = int(roll)
        except Exception:
            continue

        sigla = COR_SIGLA.get(CORES.get(color))
        if sigla is None:
            continue

        rodadas.append(str(rodada_id))
        rolls.append(roll)
        colors.append(sigla)

    return rodadas, rolls, colors


def liquidar_sinal_atual(cur, rodada_id, cor_resultado, now):
    cur.execute("""
        SELECT sinal_ativo, cor_sinal, rodada_base_sinal,
               wins, losses, whites, profit
        FROM bot_estado
        WHERE id = 1;
    """)

    state = cur.fetchone()
    if not state:
        return 0, 0, 0, 0.00

    (
        sinal_ativo,
        cor_sinal,
        rodada_base_sinal,
        wins,
        losses,
        whites,
        profit,
    ) = state

    wins = wins or 0
    losses = losses or 0
    whites = whites or 0
    profit = float(profit or 0)

    if not sinal_ativo or not cor_sinal:
        return wins, losses, whites, profit

    if cor_resultado == "W":
        whites += 1
        profit -= APOSTA_BASE
        resultado = "WHITE"
    elif cor_resultado == cor_sinal:
        wins += 1
        profit += APOSTA_BASE
        resultado = "WIN"
    else:
        losses += 1
        profit -= APOSTA_BASE
        resultado = "LOSS"

    if rodada_base_sinal:
        cur.execute("""
            UPDATE estrategia_sinais
            SET rodada_resultado = %s,
                cor_resultado = %s,
                resultado = %s,
                resolvido_em = %s
            WHERE rodada_base = %s
              AND resultado = 'PENDENTE';
        """, (
            str(rodada_id),
            cor_resultado,
            resultado,
            now,
            str(rodada_base_sinal),
        ))

    print(
        f"📊 SINAL RESOLVIDO | {sinal_ativo} | "
        f"prev={cor_sinal} | resultado={cor_resultado} | {resultado}",
        flush=True,
    )

    return wins, losses, whites, profit


def processar_novo_resultado(rodada_id, color, roll):
    """
    Chamado pelo collector depois que a rodada foi salva no Neon.
    Liquida o sinal anterior e cria, se houver gatilho, um sinal
    para a próxima rodada.
    """
    if not init_engine_db():
        return None

    conn = get_db_connection()
    if not conn:
        return None

    try:
        now = datetime.now()
        cor_resultado = COR_SIGLA.get(CORES.get(int(color)))

        if cor_resultado is None:
            conn.close()
            return None

        with conn.cursor() as cur:
            wins, losses, whites, profit = liquidar_sinal_atual(
                cur, str(rodada_id), cor_resultado, now
            )

            rodadas, rolls, colors = carregar_historico(conn)

            if not rodadas:
                conn.commit()
                conn.close()
                return None

            # A rodada recém-chegada não participa do gatilho que
            # criará o sinal para a próxima rodada.
            idx_atual = None
            for i in range(len(rodadas) - 1, -1, -1):
                if rodadas[i] == str(rodada_id):
                    idx_atual = i
                    break

            if idx_atual is not None:
                rolls_check = rolls[:idx_atual] + rolls[idx_atual + 1:]
                colors_check = colors[:idx_atual] + colors[idx_atual + 1:]
            else:
                rolls_check = rolls[:-1]
                colors_check = colors[:-1]

            novo_sinal = get_active_signal(rolls_check, colors_check)

            sinal_ativo = None
            cor_sinal = None
            rodada_base_sinal = None
            estrategia = None

            if novo_sinal:
                estrategia, cor_sinal = novo_sinal
                sinal_ativo = estrategia
                rodada_base_sinal = str(rodada_id)

                cur.execute("""
                    INSERT INTO estrategia_sinais
                        (rodada_base, estrategia, cor_prevista, resultado)
                    VALUES (%s, %s, %s, 'PENDENTE');
                """, (
                    str(rodada_id),
                    estrategia,
                    cor_sinal,
                ))

                print("")
                print("=" * 70)
                print("🎯 NOVO SINAL — MOTOR ATUALIZADO")
                print("=" * 70)
                print(f"Base       : {rodada_id}")
                print(f"Estratégia : {estrategia}")
                print(f"Previsão   : {cor_sinal}")
                print("Entrada    : PRÓXIMA RODADA")
                print("=" * 70)
                print("")

            else:
                print(
                    f"🔎 MOTOR | rodada={rodada_id} | "
                    f"nenhum gatilho encontrado",
                    flush=True,
                )

            cur.execute("""
                UPDATE bot_estado
                SET wins = %s,
                    losses = %s,
                    whites = %s,
                    profit = %s,
                    sinal_ativo = %s,
                    cor_sinal = %s,
                    ultima_rodada_processada = %s,
                    rodada_base_sinal = %s,
                    motor_ativo = TRUE,
                    ultima_estrategia = %s,
                    atualizado_em = %s
                WHERE id = 1;
            """, (
                wins,
                losses,
                whites,
                profit,
                sinal_ativo,
                cor_sinal,
                str(rodada_id),
                rodada_base_sinal,
                estrategia,
                now,
            ))

        conn.commit()
        conn.close()

        return {
            "ativo": True,
            "estrategia": estrategia,
            "cor": cor_sinal,
            "wins": wins,
            "losses": losses,
            "whites": whites,
            "profit": profit,
        }

    except Exception as e:
        print(f"❌ MOTOR: erro processando rodada {rodada_id}: {e}")
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return None


def obter_status_motor():
    conn = get_db_connection()

    if not conn:
        return {
            "ativo": False,
            "sinal": None,
            "cor": None,
            "estrategia": None,
        }

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT motor_ativo, sinal_ativo, cor_sinal,
                       ultima_estrategia, wins, losses, whites, profit
                FROM bot_estado
                WHERE id = 1;
            """)
            row = cur.fetchone()

        conn.close()

        if not row:
            return {
                "ativo": True,
                "sinal": None,
                "cor": None,
                "estrategia": None,
            }

        return {
            "ativo": bool(row[0]),
            "sinal": row[1],
            "cor": row[2],
            "estrategia": row[3],
            "wins": row[4] or 0,
            "losses": row[5] or 0,
            "whites": row[6] or 0,
            "profit": float(row[7] or 0),
        }

    except Exception as e:
        print(f"❌ MOTOR: erro consultando status: {e}")
        try:
            conn.close()
        except Exception:
            pass
        return {
            "ativo": False,
            "sinal": None,
            "cor": None,
            "estrategia": None,
        }


if __name__ == "__main__":
    if init_engine_db():
        print("✅ Motor atualizado inicializado.")
        print("🟢 Estratégias ativas:")
        print("   1. VI → VI → R")
        print("   2. VI → PP → P")
        print("   3. VI → VI → VI → R")
        print("   4. ⚪ WHITE + 13 → R")
