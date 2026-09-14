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
        print("❌ MOTOR: DATABASE_URL não configurada.", flush=True)
        return None
    try:
        if "sslmode=" not in database_url:
            separator = "&" if "?" in database_url else "?"
            database_url = f"{database_url}{separator}sslmode=require"
        return psycopg2.connect(database_url, connect_timeout=15)
    except Exception as e:
        print(f"❌ MOTOR: erro PostgreSQL: {e}", flush=True)
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
                    motor_ativo BOOLEAN DEFAULT FALSE,
                    ultima_estrategia VARCHAR(150),
                    inicio_sessao TIMESTAMP,
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
                ("motor_ativo", "BOOLEAN DEFAULT FALSE"),
                ("ultima_estrategia", "VARCHAR(150)"),
                ("inicio_sessao", "TIMESTAMP"),
                ("atualizado_em", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
            ]
            for name, definition in columns:
                cur.execute(
                    f"ALTER TABLE bot_estado ADD COLUMN IF NOT EXISTS {name} {definition};"
                )

            cur.execute("""
                INSERT INTO bot_estado
                    (id, wins, losses, whites, profit, motor_ativo)
                VALUES (1, 0, 0, 0, 0.00, FALSE)
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

            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_estrategia_sinais_base ON estrategia_sinais(rodada_base);"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_estrategia_sinais_estrategia ON estrategia_sinais(estrategia);"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_estrategia_sinais_resultado ON estrategia_sinais(resultado);"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_estrategia_sinais_criado_em ON estrategia_sinais(criado_em);"
            )

            # Compatibilidade: qualquer WHITE antigo vira LOSS operacional.
            cur.execute("""
                UPDATE estrategia_sinais
                SET resultado = 'LOSS'
                WHERE resultado = 'WHITE';
            """)

        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"❌ MOTOR: erro inicializando banco: {e}", flush=True)
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
            color = int(color)
            roll = int(roll)
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
    """Retorna uma única estratégia, obedecendo à prioridade operacional."""
    colors = list(colors)
    rolls = list(rolls)
    n = len(colors)
    if n < 2:
        return None

    # Estratégias V4 — prioridade principal.
    # PRR precisa vir antes das regras que poderiam capturar seus sufixos.
    if n >= 3 and colors[-3:] == ["P", "R", "R"]:
        return ("PRR → R", "R")

    if n >= 4 and colors[-4:] == ["R", "R", "P", "P"]:
        return ("RRPP → R", "R")

    if n >= 3 and colors[-3:] == ["R", "R", "P"]:
        return ("RRP → P", "P")

    # Estratégias antigas mantidas.
    # A regra de 3 vermelhos vem antes da de 2 para que não fique totalmente
    # mascarada pelo sufixo RR.
    if n >= 3 and colors[-3:] == ["R", "R", "R"]:
        return ("VI → VI → VI → R", "R")

    if colors[-2:] == ["R", "R"]:
        return ("VI → VI → R", "R")

    if n >= 3 and colors[-3:] == ["R", "P", "P"]:
        return ("VI → PP → P", "P")

    # W + 13 → R:
    # a rodada anterior é W e a rodada-base terminou com roll 13.
    if n >= 2 and colors[-2] == "W" and rolls[-1] == 13:
        return ("⚪ WHITE + 13 → R", "R")

    if n >= 5:
        last_5 = colors[-5:]
        est3 = {
            ("R", "R", "R", "P", "R"): "R",
            ("R", "R", "P", "P", "R"): "R",
            ("P", "R", "P", "R", "R"): "P",
            ("R", "R", "R", "P", "P"): "R",
        }
        if tuple(last_5) in est3:
            return ("EST 3 (Franco-Atirador)", est3[tuple(last_5)])

    if n >= 6:
        last_6 = colors[-6:]
        est5 = {
            ("R", "P", "R", "R", "P", "P"): "P",
            ("P", "R", "P", "R", "P", "P"): "P",
            ("R", "R", "P", "R", "R", "P"): "P",
            ("R", "P", "P", "R", "R", "R"): "R",
            ("P", "P", "P", "R", "P", "R"): "R",
            ("R", "R", "R", "R", "P", "R"): "R",
        }
        if tuple(last_6) in est5:
            return ("EST 5 (Mina Oculta)", est5[tuple(last_6)])

    return None


def _resultado_do_sinal(cor_prevista, cor_resultado):
    # WHITE é LOSS operacional. Só a cor prevista exata é WIN.
    return RESULTADO_WIN if cor_resultado == cor_prevista else RESULTADO_LOSS


def resolver_sinal_por_rodada(
    cur,
    sinal_id,
    rodada_base,
    estrategia,
    cor_prevista,
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
    """, (
        str(rodada_resultado),
        cor_resultado,
        resultado,
        now,
        sinal_id,
    ))

    if cur.rowcount != 1:
        return False

    print(
        "📊 SINAL RESOLVIDO | "
        f"id={sinal_id} | base={rodada_base} | "
        f"resultado={rodada_resultado} | estratégia={estrategia} | "
        f"prev={cor_prevista} | real={cor_resultado} | {resultado}",
        flush=True,
    )
    return True


def _proxima_rodada_complete(cur, base_db_id):
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
    return cur.fetchone()


def reconciliar_sinais_pendentes(cur, now, limite=5000):
    """Resolve PENDENTES pela primeira rodada complete posterior à base."""
    cur.execute("""
        SELECT s.id, s.rodada_base, s.estrategia, s.cor_prevista,
               b.id AS base_db_id
        FROM estrategia_sinais s
        JOIN blaze_historico b
          ON b.rodada_id = s.rodada_base
         AND b.status = 'complete'
        WHERE s.resultado = 'PENDENTE'
        ORDER BY s.id ASC
        LIMIT %s;
    """, (limite,))
    pendentes = cur.fetchall()

    resolvidos = 0
    sem_resultado = 0

    for sinal_id, rodada_base, estrategia, cor_prevista, base_db_id in pendentes:
        row = _proxima_rodada_complete(cur, base_db_id)
        if not row:
            sem_resultado += 1
            continue

        resultado_db_id, rodada_resultado, color_resultado, _roll = row
        cor_resultado = cor_para_sigla(color_resultado)
        if cor_resultado is None:
            sem_resultado += 1
            continue

        if resolver_sinal_por_rodada(
            cur,
            sinal_id,
            rodada_base,
            estrategia,
            cor_prevista,
            rodada_resultado,
            cor_resultado,
            now,
        ):
            resolvidos += 1

    if resolvidos:
        print(
            "🔄 RECONCILIAÇÃO | "
            f"resolvidos={resolvidos} | sem_resultado={sem_resultado} | "
            f"pendentes_lidos={len(pendentes)}",
            flush=True,
        )

    return {
        "resolvidos": resolvidos,
        "sem_resultado": sem_resultado,
        "pendentes_lidos": len(pendentes),
    }


def reconciliar_rodada_atual(cur, rodada_id, now):
    """Garante que a rodada atual seja usada somente quando for a imediata posterior da base."""
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
        row = _proxima_rodada_complete(cur, base_db_id)
        if not row:
            continue

        resultado_db_id, rodada_resultado, color_resultado, _roll = row
        if int(resultado_db_id) != atual_id:
            continue

        cor_resultado = cor_para_sigla(color_resultado)
        if cor_resultado is None:
            continue

        if resolver_sinal_por_rodada(
            cur,
            sinal_id,
            rodada_base,
            estrategia,
            cor_prevista,
            rodada_resultado,
            cor_resultado,
            now,
        ):
            resolvidos += 1

    return resolvidos


def recalcular_bot_estado(cur, rodada_atual=None, now=None):
    """bot_estado é resumo global; o dashboard calcula a sessão separadamente."""
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

    cur.execute("SELECT motor_ativo FROM bot_estado WHERE id = 1;")
    state = cur.fetchone()
    motor_ativo = bool(state[0]) if state else False

    cur.execute("""
        SELECT estrategia, cor_prevista, rodada_base
        FROM estrategia_sinais
        WHERE resultado = 'PENDENTE'
        ORDER BY id DESC
        LIMIT 1;
    """)
    sinal = cur.fetchone()

    if sinal and motor_ativo:
        estrategia_ativa, cor_sinal, rodada_base_sinal = sinal
        sinal_ativo = estrategia_ativa
    else:
        estrategia_ativa = None
        cor_sinal = None
        rodada_base_sinal = None
        sinal_ativo = None

    profit = (wins - losses) * APOSTA_BASE
    now = now or datetime.now()

    cur.execute("""
        UPDATE bot_estado
        SET wins = %s,
            losses = %s,
            whites = %s,
            profit = %s,
            sinal_ativo = %s,
            cor_sinal = %s,
            ultima_rodada_processada = COALESCE(%s, ultima_rodada_processada),
            rodada_base_sinal = %s,
            ultima_estrategia = %s,
            atualizado_em = %s
        WHERE id = 1;
    """, (
        wins,
        losses,
        whites_internos,
        profit,
        sinal_ativo,
        cor_sinal,
        str(rodada_atual) if rodada_atual is not None else None,
        rodada_base_sinal,
        estrategia_ativa,
        now,
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
        "motor_ativo": motor_ativo,
    }


def processar_novo_resultado(rodada_id, color, roll):
    """Processa uma rodada já persistida no blaze_historico."""
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
            cur.execute("""
                SELECT id
                FROM blaze_historico
                WHERE rodada_id = %s
                  AND status = 'complete'
                LIMIT 1;
            """, (rodada_id,))
            rodada_db = cur.fetchone()
            if not rodada_db:
                raise RuntimeError(f"Rodada {rodada_id} não encontrada como complete.")
            rodada_db_id = int(rodada_db[0])

            cur.execute("SELECT motor_ativo, ultima_rodada_processada FROM bot_estado WHERE id = 1;")
            estado = cur.fetchone()
            motor_ativo = bool(estado[0]) if estado else False
            ultima_processada = str(estado[1]) if estado and estado[1] is not None else None

            # Primeiro: reconcilia qualquer PENDENTE que já tenha resultado disponível.
            reconciliar_sinais_pendentes(cur, now)
            reconciliar_rodada_atual(cur, rodada_id, now)

            # Idempotência do callback.
            if ultima_processada == rodada_id:
                resumo = recalcular_bot_estado(cur, rodada_id, now)
                conn.commit()
                print(f"♻️ MOTOR | rodada {rodada_id} já processada; reconciliação executada.", flush=True)
                return {
                    "ativo": motor_ativo,
                    "estrategia": resumo["estrategia"],
                    "cor": resumo["cor_sinal"],
                    "wins": resumo["wins"],
                    "losses": resumo["losses"],
                    "pendentes": resumo["pendentes"],
                    "profit": resumo["profit"],
                    "reconciliado": True,
                }

            # Sempre atualiza a última rodada processada, mesmo se o motor estiver pausado.
            # Isso evita reprocessamento infinito do mesmo callback.
            if motor_ativo:
                _ids, _rodadas, rolls, colors = carregar_historico(
                    conn,
                    ate_id=rodada_db_id - 1,
                )
                novo_sinal = get_active_signal(rolls, colors)

                if novo_sinal:
                    estrategia, cor_sinal = novo_sinal

                    # Uma rodada-base só pode gerar um sinal operacional.
                    cur.execute("""
                        SELECT id, resultado
                        FROM estrategia_sinais
                        WHERE rodada_base = %s
                        ORDER BY id DESC
                        LIMIT 1;
                    """, (rodada_id,))
                    existente = cur.fetchone()

                    if not existente:
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
                            f"♻️ SINAL JÁ EXISTE | base={rodada_id} | "
                            f"id={existente[0]} | status={existente[1]}",
                            flush=True,
                        )
                else:
                    print(
                        f"🔎 MOTOR | rodada={rodada_id} | nenhum gatilho encontrado",
                        flush=True,
                    )
            else:
                print(
                    f"⏸️ MOTOR PAUSADO | rodada={rodada_id} | "
                    "coleta registrada, nenhuma nova previsão criada",
                    flush=True,
                )

            resumo = recalcular_bot_estado(cur, rodada_id, now)

        conn.commit()
        conn.close()

        return {
            "ativo": motor_ativo,
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
    """Reconciliação manual completa; não altera o histórico das rodadas."""
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
            "ativo": False,
            "sinal": None,
            "cor": None,
            "estrategia": None,
            "wins": 0,
            "losses": 0,
            "pendentes": 0,
            "profit": 0.0,
        }

    try:
        with conn.cursor() as cur:
            resumo = recalcular_bot_estado(cur, now=datetime.now())
        conn.commit()
        conn.close()
        return {
            "ativo": resumo["motor_ativo"],
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
            "ativo": False,
            "sinal": None,
            "cor": None,
            "estrategia": None,
            "wins": 0,
            "losses": 0,
            "pendentes": 0,
            "profit": 0.0,
        }


if __name__ == "__main__":
    if init_engine_db():
        print("✅ Motor inicializado.")
        print("🟢 Estratégias ativas:")
        print("   1. PRR → R")
        print("   2. RRPP → R")
        print("   3. RRP → P")
        print("   4. VI → VI → VI → R")
        print("   5. VI → VI → R")
        print("   6. VI → PP → P")
        print("   7. ⚪ WHITE + 13 → R")
        print("   8. EST 3 (Franco-Atirador)")
        print("   9. EST 5 (Mina Oculta)")
        print("\n🔄 Executando reconciliação inicial...")
        reconciliar_todos_sinais()
