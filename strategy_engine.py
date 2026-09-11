# ================================================================
# BLAZE BOT — MOTOR DAS 5 ESTRATÉGIAS
# ================================================================
# Este módulo NÃO coleta dados da Blaze.
#
# Fluxo:
#   collector.py -> salva resultado -> processar_novo_resultado()
#                                  -> liquida sinal anterior
#                                  -> analisa histórico
#                                  -> gera sinal para próxima rodada
#
# As regras abaixo preservam as 5 estratégias do projeto original.
# ================================================================

import os
from datetime import datetime

import psycopg2


APOSTA_BASE = 1.00

CORES = {
    0: "BRANCO",
    1: "VERMELHO",
    2: "PRETO",
}

COR_SIGLA = {
    "BRANCO": "W",
    "VERMELHO": "R",
    "PRETO": "B",
}


# ================================================================
# BANCO
# ================================================================

def get_db_connection():
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        print("❌ MOTOR: DATABASE_URL não configurada.")
        return None

    try:
        if "sslmode=" not in database_url:
            separator = "&" if "?" in database_url else "?"
            database_url = f"{database_url}{separator}sslmode=require"

        return psycopg2.connect(
            database_url,
            connect_timeout=15,
        )

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

            # Compatibilidade com versões anteriores do bot_estado.
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
                INSERT INTO bot_estado (
                    id, wins, losses, whites, profit, motor_ativo
                )
                VALUES (1, 0, 0, 0, 0.00, TRUE)
                ON CONFLICT (id) DO NOTHING;
            """)

            # Registro individual dos sinais.
            # Isso nos permitirá medir cada estratégia separadamente.
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


# ================================================================
# MOTOR DAS 5 ESTRATÉGIAS
# ================================================================

def get_active_signal(rolls, colors):
    """
    Retorna:
        (nome_da_estrategia, cor_prevista)
    ou None.

    A ordem de prioridade é a mesma do projeto original:
        EST 2 -> EST 3 -> EST 1 -> EST 4 -> EST 5
    """

    n = len(rolls)

    if n < 4:
        return None

    # ------------------------------------------------------------
    # EST 2 — OPERACIONAL
    # ------------------------------------------------------------
    last_4 = list(colors)[-4:]

    if last_4 == ["R", "R", "B", "B"]:
        return ("EST 2 (Operacional)", "R")

    if last_4 == ["R", "B", "B", "R"]:
        return ("EST 2 (Operacional)", "R")

    if last_4 == ["B", "B", "R", "R"]:
        return ("EST 2 (Operacional)", "R")

    if last_4 == ["B", "R", "R", "B"]:
        return ("EST 2 (Operacional)", "B")

    # ------------------------------------------------------------
    # EST 3 — FRANCO-ATIRADOR
    # ------------------------------------------------------------
    if n >= 5:

        last_5 = list(colors)[-5:]

        if last_5 == ["R", "R", "R", "B", "R"]:
            return ("EST 3 (Franco-Atirador)", "R")

        if last_5 == ["R", "R", "B", "B", "R"]:
            return ("EST 3 (Franco-Atirador)", "R")

        if last_5 == ["B", "R", "B", "R", "R"]:
            return ("EST 3 (Franco-Atirador)", "B")

        if last_5 == ["R", "R", "R", "B", "B"]:
            return ("EST 3 (Franco-Atirador)", "R")

    # ------------------------------------------------------------
    # EST 1 — SNIPER PAR / ÍMPAR
    # ------------------------------------------------------------
    if n >= 6:

        last_6_rolls = list(rolls)[-6:]

        if all(r != 0 and r % 2 == 0 for r in last_6_rolls):
            return ("EST 1 (Sniper Par)", "R")

        if all(r != 0 and r % 2 != 0 for r in last_6_rolls):
            return ("EST 1 (Sniper Ímpar)", "B")

    # ------------------------------------------------------------
    # EST 4 — BALA DE PRATA
    # ------------------------------------------------------------
        last_6_colors = list(colors)[-6:]

        if last_6_colors == ["B", "B", "R", "B", "R", "R"]:
            return ("EST 4 (Bala de Prata)", "B")

        if last_6_colors == ["B", "R", "R", "R", "B", "B"]:
            return ("EST 4 (Bala de Prata)", "R")

        if last_6_colors == ["R", "R", "R", "B", "B", "R"]:
            return ("EST 4 (Bala de Prata)", "R")

        if last_6_colors == ["R", "R", "B", "B", "B", "B"]:
            return ("EST 4 (Bala de Prata)", "B")

    # ------------------------------------------------------------
    # EST 5 — MINA OCULTA
    # ------------------------------------------------------------
        if last_6_colors == ["R", "B", "R", "R", "B", "B"]:
            return ("EST 5 (Mina Oculta)", "B")

        if last_6_colors == ["B", "R", "B", "R", "B", "B"]:
            return ("EST 5 (Mina Oculta)", "B")

        if last_6_colors == ["R", "R", "B", "R", "R", "B"]:
            return ("EST 5 (Mina Oculta)", "B")

        if last_6_colors == ["R", "B", "B", "R", "R", "R"]:
            return ("EST 5 (Mina Oculta)", "R")

        if last_6_colors == ["B", "B", "B", "R", "B", "R"]:
            return ("EST 5 (Mina Oculta)", "R")

        if last_6_colors == ["R", "R", "R", "R", "B", "R"]:
            return ("EST 5 (Mina Oculta)", "R")

    return None


# ================================================================
# HISTÓRICO
# ================================================================

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

    rodadas = []
    rolls = []
    colors = []

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


# ================================================================
# LIQUIDAÇÃO DO SINAL ANTERIOR
# ================================================================

def liquidar_sinal_atual(cur, rodada_id, cor_resultado, now):
    cur.execute("""
        SELECT
            sinal_ativo,
            cor_sinal,
            rodada_base_sinal,
            wins,
            losses,
            whites,
            profit
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

    # Resolve o registro do sinal correspondente.
    if rodada_base_sinal:
        cur.execute("""
            UPDATE estrategia_sinais
            SET
                rodada_resultado = %s,
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
        f"📊 SINAL RESOLVIDO | "
        f"{sinal_ativo} | "
        f"prev={cor_sinal} | "
        f"resultado={cor_resultado} | "
        f"{resultado}",
        flush=True,
    )

    return wins, losses, whites, profit


# ================================================================
# PROCESSAMENTO DE CADA NOVO RESULTADO
# ================================================================

def processar_novo_resultado(rodada_id, color, roll):
    """
    Executado SOMENTE depois que o collector confirmou que a rodada
    foi inserida no Neon.

    Isso garante que o motor trabalhe com o mesmo histórico persistido
    pelo coletor.
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

            # --------------------------------------------------------
            # 1. Liquida o sinal anterior
            # --------------------------------------------------------
            wins, losses, whites, profit = liquidar_sinal_atual(
                cur,
                str(rodada_id),
                cor_resultado,
                now,
            )

            # --------------------------------------------------------
            # 2. Histórico completo, incluindo o resultado atual.
            #    Para prever a próxima rodada, retiramos o último.
            # --------------------------------------------------------
            rodadas, rolls, colors = carregar_historico(conn)

            if not rodadas:
                conn.commit()
                conn.close()
                return None

            # O histórico deve terminar na rodada que acabou de chegar.
            # Mesmo que a ordenação do banco seja diferente, removemos
            # exatamente a rodada atual.
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

            # --------------------------------------------------------
            # 3. Procura novo gatilho
            # --------------------------------------------------------
            novo_sinal = get_active_signal(
                rolls_check,
                colors_check,
            )

            sinal_ativo = None
            cor_sinal = None
            rodada_base_sinal = None
            estrategia = None

            if novo_sinal:
                estrategia, cor_sinal = novo_sinal
                sinal_ativo = estrategia
                rodada_base_sinal = str(rodada_id)

                cur.execute("""
                    INSERT INTO estrategia_sinais (
                        rodada_base,
                        estrategia,
                        cor_prevista,
                        resultado
                    )
                    VALUES (%s, %s, %s, 'PENDENTE');
                """, (
                    str(rodada_id),
                    estrategia,
                    cor_sinal,
                ))

                print("")
                print("=" * 70)
                print("🎯 NOVO SINAL DAS ESTRATÉGIAS")
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

            # --------------------------------------------------------
            # 4. Persiste estado
            # --------------------------------------------------------
            cur.execute("""
                UPDATE bot_estado
                SET
                    wins = %s,
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


# ================================================================
# STATUS
# ================================================================

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
                SELECT
                    motor_ativo,
                    sinal_ativo,
                    cor_sinal,
                    ultima_estrategia,
                    wins,
                    losses,
                    whites,
                    profit
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
        print("✅ Motor das 5 estratégias inicializado.")
        print("🟢 Estratégias disponíveis: EST 1, EST 2, EST 3, EST 4, EST 5")
