import os
from datetime import datetime

import psycopg2


APOSTA_BASE = 1.00

CORES = {0: "BRANCO", 1: "VERMELHO", 2: "PRETO"}
COR_SIGLA = {"BRANCO": "W", "VERMELHO": "R", "PRETO": "P"}

RESULTADO_PENDENTE = "PENDENTE"
RESULTADO_WIN = "WIN"
RESULTADO_LOSS = "LOSS"


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
                cur.execute(f"ALTER TABLE bot_estado ADD COLUMN IF NOT EXISTS {name} {definition};")

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
            cur.execute("CREATE INDEX IF NOT EXISTS idx_estrategia_sinais_base ON estrategia_sinais(rodada_base);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_estrategia_sinais_estrategia ON estrategia_sinais(estrategia);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_estrategia_sinais_resultado ON estrategia_sinais(resultado);")

            # Regra operacional: WHITE contra aposta R/P = LOSS.
            # Normaliza registros antigos para não criar uma terceira categoria.
            cur.execute("""
                UPDATE estrategia_sinais
                SET resultado = 'LOSS'
                WHERE resultado = 'WHITE';
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


def cor_para_sigla(color):
    try:
        return COR_SIGLA.get(CORES.get(int(color)))
    except (TypeError, ValueError):
        return None


def carregar_historico(conn, ate_id=None):
    """Carrega apenas rodadas completas e, quando solicitado, apenas até ate_id."""
    with conn.cursor() as cur:
        if ate_id is None:
            cur.execute("""
                SELECT id, rodada_id, color, roll
                FROM blaze_historico
                WHERE status = 'complete'
                  AND color IN (0, 1, 2)
                  AND roll IS NOT NULL
                ORDER BY id ASC;
            """)
        else:
            cur.execute("""
                SELECT id, rodada_id, color, roll
                FROM blaze_historico
                WHERE status = 'complete'
                  AND color IN (0, 1, 2)
                  AND roll IS NOT NULL
                  AND id <= %s
                ORDER BY id ASC;
            """, (ate_id,))
        rows = cur.fetchall()

    ids, rodadas, rolls, colors = [], [], [], []
    for db_id, rodada_id, color, roll in rows:
        try:
            color, roll = int(color), int(roll)
        except (TypeError, ValueError):
            continue
        sigla = cor_para_sigla(color)
        if sigla is None:
            continue
        ids.append(int(db_id))
        rodadas.append(str(rodada_id))
        rolls.append(roll)
        colors.append(sigla)
    return ids, rodadas, rolls, colors


def get_active_signal(rolls, colors):
    """Detecta uma única estratégia respeitando a prioridade definida."""
    rolls = list(rolls)
    colors = list(colors)
    n = len(colors)
    if n < 2:
        return None

    # 1) PRR -> R
    if n >= 3 and colors[-3:] == ["P", "R", "R"]:
        return ("PRR → R", "R")

    # 2) RRPP -> R (antes de RRP, pois RRP é sufixo de RRPP)
    if n >= 4 and colors[-4:] == ["R", "R", "P", "P"]:
        return ("RRPP → R", "R")

    # 3) RRP -> P
    if n >= 3 and colors[-3:] == ["R", "R", "P"]:
        return ("RRP → P", "P")

    # 4) VI -> VI -> R
    if colors[-2:] == ["R", "R"]:
        return ("VI → VI → R", "R")

    # 5) VI -> PP -> P
    if n >= 3 and colors[-3:] == ["R", "P", "P"]:
        return ("VI → PP → P", "P")

    # 6) VI -> VI -> VI -> R
    # Fica depois de R,R para preservar a prioridade do motor.
    if n >= 3 and colors[-3:] == ["R", "R", "R"]:
        return ("VI → VI → VI → R", "R")

    # 7) WHITE + 13 -> R
    if n >= 2 and colors[-2] == "W" and rolls[-1] == 13:
        return ("⚪ WHITE + 13 → R", "R")

    # 8) EST 3 — Franco-Atirador
    if n >= 5:
        last_5 = colors[-5:]
        if last_5 == ["R", "R", "R", "P", "R"]:
            return ("EST 3 (Franco-Atirador)", "R")
        if last_5 == ["R", "R", "P", "P", "R"]:
            return ("EST 3 (Franco-Atirador)", "R")
        if last_5 == ["P", "R", "P", "R", "R"]:
            return ("EST 3 (Franco-Atirador)", "P")
        if last_5 == ["R", "R", "R", "P", "P"]:
            return ("EST 3 (Franco-Atirador)", "R")

    # 9) EST 5 — Mina Oculta
    if n >= 6:
        last_6 = colors[-6:]
        if last_6 == ["R", "P", "R", "R", "P", "P"]:
            return ("EST 5 (Mina Oculta)", "P")
        if last_6 == ["P", "R", "P", "R", "P", "P"]:
            return ("EST 5 (Mina Oculta)", "P")
        if last_6 == ["R", "R", "P", "R", "R", "P"]:
            return ("EST 5 (Mina Oculta)", "P")
        if last_6 == ["R", "P", "P", "R", "R", "R"]:
            return ("EST 5 (Mina Oculta)", "R")
        if last_6 == ["P", "P", "P", "R", "P", "R"]:
            return ("EST 5 (Mina Oculta)", "R")
        if last_6 == ["R", "R", "R", "R", "P", "R"]:
            return ("EST 5 (Mina Oculta)", "R")

    return None


def _resultado_do_sinal(cor_prevista, cor_resultado):
    """R/P correto = WIN; qualquer outro resultado, inclusive W, = LOSS."""
    return RESULTADO_WIN if cor_resultado == cor_prevista else RESULTADO_LOSS


def resolver_sinal_por_rodada(
    cur,
    sinal_id,
    rodada_base,
    estrategia,
    cor_prevista,
    base_db_id,
    resultado_db_id,
    rodada_resultado,
    cor_resultado,
    now,
):
    resultado = _resultado_do_sinal(cor_prevista, cor_resultado)
    cur.execute("""
        UPDATE estrategia_sinais
        SET rodada_resultado = %s,
            cor_resultado = %s,
            resultado = %s,
            resolvido_em = %s
        WHERE id = %s
          AND resultado = 'PENDENTE';
    """, (str(rodada_resultado), cor_resultado, resultado, now, sinal_id))

    if cur.rowcount != 1:
        return False

    print(
        "📊 SINAL RESOLVIDO | "
        f"id={sinal_id} | base={rodada_base} | resultado={rodada_resultado} | "
        f"estrategia={estrategia} | prev={cor_prevista} | real={cor_resultado} | {resultado}",
        flush=True,
    )
    return True


def reconciliar_sinais_pendentes(cur, now, limite=5000):
    """
    Reconcilia cada PENDENTE com a primeira rodada complete depois da base.
    Não depende do callback ter sido chamado para cada rodada.
    """
    cur.execute("""
        SELECT s.id, s.rodada_base, s.estrategia, s.cor_prevista, b.id AS base_db_id
        FROM estrategia_sinais s
        LEFT JOIN blaze_historico b
          ON b.rodada_id = s.rodada_base
         AND b.status = 'complete'
        WHERE s.resultado = 'PENDENTE'
        ORDER BY s.id ASC
        LIMIT %s;
    """, (limite,))
    pendentes = cur.fetchall()

    resolvidos = 0
    sem_resultado = 0
    base_inexistente = 0

    for sinal_id, rodada_base, estrategia, cor_prevista, base_db_id in pendentes:
        if base_db_id is None:
            base_inexistente += 1
            continue

        cur.execute("""
            SELECT id, rodada_id, color, roll
            FROM blaze_historico
            WHERE status = 'complete'
              AND id > %s
              AND color IN (0, 1, 2)
              AND roll IS NOT NULL
            ORDER BY id ASC
            LIMIT 1;
        """, (base_db_id,))
        row = cur.fetchone()
        if not row:
            sem_resultado += 1
            continue

        resultado_db_id, rodada_resultado, color_resultado, roll_resultado = row
        cor_resultado = cor_para_sigla(color_resultado)
        if cor_resultado is None:
            sem_resultado += 1
            continue

        if resolver_sinal_por_rodada(
            cur, sinal_id, rodada_base, estrategia, cor_prevista,
            base_db_id, resultado_db_id, rodada_resultado, cor_resultado, now
        ):
            resolvidos += 1

    if resolvidos:
        print(
            "🔄 RECONCILIAÇÃO | "
            f"resolvidos={resolvidos} | sem_resultado={sem_resultado} | "
            f"base_inexistente={base_inexistente}",
            flush=True,
        )

    return {
        "resolvidos": resolvidos,
        "sem_resultado": sem_resultado,
        "base_inexistente": base_inexistente,
        "pendentes_lidos": len(pendentes),
    }


def reconciliar_rodada_atual(cur, rodada_id, now):
    """Resolve somente sinais cujo resultado imediato é a rodada atual."""
    cur.execute("""
        SELECT id
        FROM blaze_historico
        WHERE rodada_id = %s
          AND status = 'complete'
        LIMIT 1;
    """, (str(rodada_id),))
    atual = cur.fetchone()
    if not atual:
        return 0
    atual_id = int(atual[0])

    cur.execute("""
        SELECT s.id, s.rodada_base, s.estrategia, s.cor_prevista, b.id AS base_db_id
        FROM estrategia_sinais s
        JOIN blaze_historico b
          ON b.rodada_id = s.rodada_base
         AND b.status = 'complete'
        WHERE s.resultado = 'PENDENTE'
          AND b.id < %s
        ORDER BY b.id DESC, s.id ASC;
    """, (atual_id,))

    resolvidos = 0
    for sinal_id, rodada_base, estrategia, cor_prevista, base_db_id in cur.fetchall():
        cur.execute("""
            SELECT id, rodada_id, color, roll
            FROM blaze_historico
            WHERE status = 'complete'
              AND id > %s
              AND color IN (0, 1, 2)
              AND roll IS NOT NULL
            ORDER BY id ASC
            LIMIT 1;
        """, (base_db_id,))
        row = cur.fetchone()
        if not row:
            continue

        resultado_db_id, rodada_resultado, color_resultado, roll_resultado = row
        if int(resultado_db_id) != atual_id:
            continue

        cor_resultado = cor_para_sigla(color_resultado)
        if cor_resultado is None:
            continue

        if resolver_sinal_por_rodada(
            cur, sinal_id, rodada_base, estrategia, cor_prevista,
            base_db_id, resultado_db_id, rodada_resultado, cor_resultado, now
        ):
            resolvidos += 1
    return resolvidos


def recalcular_bot_estado(cur, rodada_atual=None, now=None):
    """
    bot_estado é resumo derivado de estrategia_sinais.
    Dashboard operacional: somente WIN e LOSS.
    WHITE, quando ocorre, está em LOSS e pode ser identificado apenas
    internamente por cor_resultado='W'.
    """
    cur.execute("""
        SELECT
            COUNT(*) FILTER (WHERE resultado = 'WIN') AS wins,
            COUNT(*) FILTER (WHERE resultado = 'LOSS') AS losses,
            COUNT(*) FILTER (WHERE resultado = 'PENDENTE') AS pendentes,
            COUNT(*) FILTER (
                WHERE resultado = 'LOSS' AND cor_resultado = 'W'
            ) AS whites_internos
        FROM estrategia_sinais;
    """)
    wins, losses, pendentes, whites_internos = cur.fetchone()
    wins = int(wins or 0)
    losses = int(losses or 0)
    pendentes = int(pendentes or 0)
    whites_internos = int(whites_internos or 0)

    cur.execute("""
        SELECT estrategia, cor_prevista, rodada_base
        FROM estrategia_sinais
        WHERE resultado = 'PENDENTE'
        ORDER BY id DESC
        LIMIT 1;
    """)
    sinal = cur.fetchone()

    if sinal:
        estrategia_ativa, cor_sinal, rodada_base_sinal = sinal
        sinal_ativo = estrategia_ativa
    else:
        estrategia_ativa = None
        cor_sinal = None
        rodada_base_sinal = None
        sinal_ativo = None

    # Cada WIN +1 e cada LOSS (inclusive WHITE) -1.
    profit = (wins * APOSTA_BASE) - (losses * APOSTA_BASE)
    now = now or datetime.now()

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
        wins, losses, whites_internos, profit,
        sinal_ativo, cor_sinal,
        str(rodada_atual) if rodada_atual is not None else None,
        rodada_base_sinal, estrategia_ativa, now,
    ))

    return {
        "wins": wins,
        "losses": losses,
        "pendentes": pendentes,
        "whites_internos": whites_internos,
        "profit": float(profit),
        "sinal_ativo": sinal_ativo,
        "cor_sinal": cor_sinal,
        "rodada_base_sinal": rodada_base_sinal,
        "estrategia": estrategia_ativa,
    }


def processar_novo_resultado(rodada_id, color, roll):
    """
    Chamado pelo collector depois que a rodada foi persistida.

    O resultado de um sinal nunca é escolhido pela ordem dos callbacks.
    Ele é encontrado pela sequência persistida em blaze_historico:
    a primeira rodada complete com id maior que a rodada-base.
    """
    if not init_engine_db():
        return None

    conn = get_db_connection()
    if not conn:
        return None

    try:
        rodada_id = str(rodada_id)
        color = int(color)
        roll = int(roll)
        cor_resultado = cor_para_sigla(color)

        if cor_resultado is None:
            print(f"⚠️ MOTOR: cor inválida na rodada {rodada_id}: {color}", flush=True)
            conn.close()
            return None

        now = datetime.now()

        with conn.cursor() as cur:
            # A rodada precisa estar persistida como complete.
            cur.execute("""
                SELECT id, rodada_id, color, roll
                FROM blaze_historico
                WHERE rodada_id = %s
                  AND status = 'complete'
                LIMIT 1;
            """, (rodada_id,))
            rodada_db = cur.fetchone()
            if not rodada_db:
                raise RuntimeError(f"Rodada {rodada_id} não encontrada como complete.")
            rodada_db_id = int(rodada_db[0])

            # Verifica se o callback já foi processado.
            cur.execute("SELECT ultima_rodada_processada FROM bot_estado WHERE id = 1;")
            estado = cur.fetchone()
            ultima_processada = str(estado[0]) if estado and estado[0] is not None else None

            # 1) Reconciliação global: corrige sinais antigos deixados pendentes.
            reconciliar_sinais_pendentes(cur, now)

            # 2) Garante especificamente a resolução contra a rodada atual.
            reconciliar_rodada_atual(cur, rodada_id, now)

            # 3) Não duplica processamento da mesma rodada.
            if ultima_processada == rodada_id:
                resumo = recalcular_bot_estado(cur, rodada_id, now)
                conn.commit()
                print(
                    f"♻️ MOTOR | rodada {rodada_id} já processada; reconciliação executada.",
                    flush=True,
                )
                return {
                    "ativo": True,
                    "estrategia": resumo["estrategia"],
                    "cor": resumo["cor_sinal"],
                    "wins": resumo["wins"],
                    "losses": resumo["losses"],
                    "pendentes": resumo["pendentes"],
                    "profit": resumo["profit"],
                    "reconciliado": True,
                }

            # 4) O gatilho usa APENAS rodadas anteriores à atual.
            _ids, _rodadas, rolls, colors = carregar_historico(
                conn,
                ate_id=rodada_db_id - 1,
            )
            novo_sinal = get_active_signal(rolls, colors)
            estrategia = None
            cor_sinal = None

            if novo_sinal:
                estrategia, cor_sinal = novo_sinal

                # Uma base só pode gerar um sinal operacional.
                cur.execute("""
                    SELECT id, resultado
                    FROM estrategia_sinais
                    WHERE rodada_base = %s
                    ORDER BY id DESC
                    LIMIT 1;
                """, (rodada_id,))
                sinal_existente = cur.fetchone()

                if sinal_existente:
                    print(
                        f"♻️ SINAL JÁ EXISTE | base={rodada_id} | "
                        f"id={sinal_existente[0]} | status={sinal_existente[1]}",
                        flush=True,
                    )
                else:
                    cur.execute("""
                        INSERT INTO estrategia_sinais
                            (rodada_base, estrategia, cor_prevista, resultado)
                        VALUES (%s, %s, %s, 'PENDENTE');
                    """, (rodada_id, estrategia, cor_sinal))

                    print("\n" + "=" * 72)
                    print("🎯 NOVO SINAL")
                    print("=" * 72)
                    print(f"Base       : {rodada_id}")
                    print(f"Estratégia : {estrategia}")
                    print(f"Previsão   : {cor_sinal}")
                    print("Entrada    : PRÓXIMA RODADA")
                    print("=" * 72 + "\n")
            else:
                print(
                    f"🔎 MOTOR | rodada={rodada_id} | nenhum gatilho encontrado",
                    flush=True,
                )

            # 5) Placar sempre recalculado da tabela de sinais.
            resumo = recalcular_bot_estado(cur, rodada_id, now)

        conn.commit()
        conn.close()

        return {
            "ativo": True,
            "estrategia": resumo["estrategia"],
            "cor": resumo["cor_sinal"],
            "wins": resumo["wins"],
            "losses": resumo["losses"],
            "pendentes": resumo["pendentes"],
            "profit": resumo["profit"],
            "reconciliado": True,
        }

    except Exception as e:
        print(f"❌ MOTOR: erro processando rodada {rodada_id}: {e}", flush=True)
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return None


def reconciliar_todos_sinais():
    """Reconciliação manual completa após deploy/restart ou falha do motor."""
    if not init_engine_db():
        return None

    conn = get_db_connection()
    if not conn:
        return None

    try:
        now = datetime.now()
        with conn.cursor() as cur:
            resultado = reconciliar_sinais_pendentes(cur, now, limite=50000)
            resumo = recalcular_bot_estado(cur, now=now)
        conn.commit()
        conn.close()

        print(
            "🧾 AUDITORIA FINAL | "
            f"resolvidos={resultado['resolvidos']} | "
            f"pendentes={resumo['pendentes']} | "
            f"W={resumo['wins']} | L={resumo['losses']} | "
            f"profit={resumo['profit']:.2f}",
            flush=True,
        )
        return {**resultado, **resumo}

    except Exception as e:
        print(f"❌ AUDITORIA: erro reconciliando sinais: {e}", flush=True)
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
            "ativo": False, "sinal": None, "cor": None, "estrategia": None,
            "wins": 0, "losses": 0, "pendentes": 0, "profit": 0.0,
        }

    try:
        with conn.cursor() as cur:
            resumo = recalcular_bot_estado(cur, now=datetime.now())
        conn.commit()
        conn.close()
        return {
            "ativo": True,
            "sinal": resumo["sinal_ativo"],
            "cor": resumo["cor_sinal"],
            "estrategia": resumo["estrategia"],
            "wins": resumo["wins"],
            "losses": resumo["losses"],
            "pendentes": resumo["pendentes"],
            "profit": resumo["profit"],
        }
    except Exception as e:
        print(f"❌ MOTOR: erro consultando status: {e}", flush=True)
        try:
            conn.close()
        except Exception:
            pass
        return {
            "ativo": False, "sinal": None, "cor": None, "estrategia": None,
            "wins": 0, "losses": 0, "pendentes": 0, "profit": 0.0,
        }


if __name__ == "__main__":
    if init_engine_db():
        print("✅ Motor inicializado.")
        print("🟢 Estratégias ativas:")
        print("   1. PRR → R")
        print("   2. RRPP → R")
        print("   3. RRP → P")
        print("   4. VI → VI → R")
        print("   5. VI → PP → P")
        print("   6. VI → VI → VI → R")
        print("   7. ⚪ WHITE + 13 → R")
        print("   8. EST 3 (Franco-Atirador)")
        print("   9. EST 5 (Mina Oculta)")
        print("\n🔄 Executando reconciliação inicial...")
        reconciliar_todos_sinais()
