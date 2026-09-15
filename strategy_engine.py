import os
from datetime import datetime

import psycopg2

APOSTA_BASE = 1.00

CORES = {0: "BRANCO", 1: "VERMELHO", 2: "PRETO"}
COR_SIGLA = {"BRANCO": "W", "VERMELHO": "R", "PRETO": "P"}

RESULTADO_PENDENTE = "PENDENTE"
RESULTADO_WIN = "WIN"
RESULTADO_LOSS = "LOSS"

# ================================================================
# V8.5 — SEIS REGRAS DEFINIDAS PELO ESTUDO
#
# O contexto tem 4 rodadas observadas. A entrada é na PRÓXIMA.
# Branco continua sendo uma cor do contexto, mas se aparecer na
# rodada de entrada ele é LOSS operacional.
# ================================================================

REGRAS_V85 = {
    ("R", "W", "P", "R"): ("P", "V8.5 R1 | R-W-P-R -> P"),
    ("P", "W", "R", "P"): ("R", "V8.5 R2 | P-W-R-P -> R"),
    ("R", "W", "R", "P"): ("P", "V8.5 R3 | R-W-R-P -> P"),
    ("P", "W", "P", "P"): ("P", "V8.5 R4 | P-W-P-P -> P"),
    ("P", "W", "R", "R"): ("P", "V8.5 R5 | P-W-R-R -> P"),
    ("R", "W", "R", "R"): ("R", "V8.5 R6 | R-W-R-R -> R"),
}


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
                INSERT INTO bot_estado (id, wins, losses, whites, profit, motor_ativo)
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
                "CREATE INDEX IF NOT EXISTS idx_estrategia_sinais_base "
                "ON estrategia_sinais(rodada_base);"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_estrategia_sinais_estrategia "
                "ON estrategia_sinais(estrategia);"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_estrategia_sinais_resultado "
                "ON estrategia_sinais(resultado);"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_estrategia_sinais_criado_em "
                "ON estrategia_sinais(criado_em);"
            )

            # Migração operacional: WHITE antigo passa a ser LOSS.
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


def mapear_cor_texto(cor):
    if cor is None:
        return None
    texto = str(cor).upper().strip()
    if texto in {"R", "RED", "VERMELHO", "V", "VI"} or "VERMELHO" in texto:
        return "R"
    if texto in {"P", "BLACK", "PRETO", "B"} or "PRETO" in texto:
        return "P"
    if texto in {"W", "WHITE", "BRANCO"} or "BRANCO" in texto:
        return "W"
    return None


def obter_cor(cor_texto, color):
    return mapear_cor_texto(cor_texto) or cor_para_sigla(color)


def carregar_historico(conn, ate_id=None):
    with conn.cursor() as cur:
        query = """
            SELECT id, rodada_id, color, cor, roll
            FROM blaze_historico
            WHERE status = 'complete'
              AND color IN (0, 1, 2)
              AND roll IS NOT NULL
        """
        params = []
        if ate_id is not None:
            query += " AND id <= %s"
            params.append(ate_id)
        query += " ORDER BY id ASC;"
        cur.execute(query, params)
        rows = cur.fetchall()

    historico = []
    for db_id, rodada_id, color, cor_texto, roll in rows:
        cor = obter_cor(cor_texto, color)
        if cor is None:
            continue
        try:
            roll = int(roll)
        except (TypeError, ValueError):
            continue
        historico.append({
            "id": int(db_id),
            "rodada_id": str(rodada_id),
            "cor": cor,
            "roll": roll,
        })
    return historico


def detectar_estrategia(hist):
    """Retorna (cor_prevista, nome_estrategia) para a última janela de 4 cores."""
    if not hist or len(hist) < 4:
        return None, None

    contexto = tuple(item["cor"] for item in hist[-4:])
    regra = REGRAS_V85.get(contexto)
    if regra is None:
        return None, None

    cor_prevista, nome = regra
    return cor_prevista, nome


def get_active_signal(rolls, colors):
    """Compatibilidade: recebe listas e retorna (estratégia, previsão)."""
    if not colors or len(colors) < 4:
        return None
    contexto = tuple(list(colors)[-4:])
    regra = REGRAS_V85.get(contexto)
    if regra is None:
        return None
    cor_prevista, nome = regra
    return nome, cor_prevista


def _resultado_do_sinal(cor_prevista, cor_resultado):
    # WHITE é LOSS operacional. Só a cor prevista exata é WIN.
    return RESULTADO_WIN if cor_resultado == cor_prevista else RESULTADO_LOSS


def _proxima_rodada_complete(cur, base_db_id):
    cur.execute("""
        SELECT id, rodada_id, color, cor, roll
        FROM blaze_historico
        WHERE status = 'complete'
          AND id > %s
          AND color IN (0, 1, 2)
          AND roll IS NOT NULL
        ORDER BY id ASC
        LIMIT 1;
    """, (base_db_id,))
    return cur.fetchone()


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


def reconciliar_sinais_pendentes(cur, now, limite=5000):
    """Resolve cada pendente pela primeira rodada complete posterior à sua base."""
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

    for sinal_id, rodada_base, estrategia, cor_prevista, base_db_id in pendentes:
        row = _proxima_rodada_complete(cur, int(base_db_id))
        if not row:
            continue

        _resultado_db_id, rodada_resultado, color_resultado, cor_texto, _roll = row
        cor_resultado = obter_cor(cor_texto, color_resultado)
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
    """Mantém bot_estado como resumo global; o dashboard calcula a sessão."""
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

    cur.execute("SELECT motor_ativo, inicio_sessao FROM bot_estado WHERE id = 1;")
    state = cur.fetchone()
    motor_ativo = bool(state[0]) if state else False
    inicio_sessao = state[1] if state else None

    # Só a sessão atual pode ter sinal ativo exibido no estado operacional.
    sinal = None
    if motor_ativo and inicio_sessao:
        cur.execute("""
            SELECT estrategia, cor_prevista, rodada_base
            FROM estrategia_sinais
            WHERE resultado = 'PENDENTE'
              AND criado_em >= %s
            ORDER BY id DESC
            LIMIT 1;
        """, (inicio_sessao,))
        sinal = cur.fetchone()

    if sinal:
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


def _rodada_persistida(cur, rodada_id):
    cur.execute("""
        SELECT id, color, cor, roll
        FROM blaze_historico
        WHERE rodada_id = %s
          AND status = 'complete'
        LIMIT 1;
    """, (str(rodada_id),))
    return cur.fetchone()


def _resolver_pendente_da_rodada_atual(cur, atual_id, now):
    """Resolve pendentes cuja primeira rodada posterior é exatamente atual_id."""
    cur.execute("""
        SELECT s.id, s.rodada_base, s.estrategia, s.cor_prevista, b.id AS base_db_id
        FROM estrategia_sinais s
        JOIN blaze_historico b
          ON b.rodada_id = s.rodada_base
         AND b.status = 'complete'
        WHERE s.resultado = 'PENDENTE'
          AND b.id < %s
        ORDER BY s.id ASC;
    """, (atual_id,))

    resolvidos = 0
    for sinal_id, rodada_base, estrategia, cor_prevista, base_db_id in cur.fetchall():
        row = _proxima_rodada_complete(cur, int(base_db_id))
        if not row:
            continue
        resultado_db_id, rodada_resultado, color_resultado, cor_texto, _roll = row
        if int(resultado_db_id) != int(atual_id):
            continue

        cor_resultado = obter_cor(cor_texto, color_resultado)
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


def _criar_sinal_para_rodada_atual(cur, rodada_id, rodada_db_id):
    """Detecta somente com histórico <= rodada atual; a entrada será a próxima."""
    historico = carregar_historico_por_cursor(cur, ate_id=rodada_db_id)
    cor_prevista, estrategia = detectar_estrategia(historico)
    if not estrategia:
        return None

    cur.execute("""
        SELECT id, resultado
        FROM estrategia_sinais
        WHERE rodada_base = %s
        ORDER BY id DESC
        LIMIT 1;
    """, (str(rodada_id),))
    existente = cur.fetchone()
    if existente:
        return None

    cur.execute("""
        INSERT INTO estrategia_sinais
            (rodada_base, estrategia, cor_prevista, resultado)
        VALUES (%s, %s, %s, 'PENDENTE');
    """, (str(rodada_id), estrategia, cor_prevista))

    print("\n" + "=" * 72)
    print("🎯 NOVO SINAL V8.5")
    print("=" * 72)
    print(f"Base       : {rodada_id}")
    print(f"Estratégia : {estrategia}")
    print(f"Previsão   : {cor_prevista}")
    print("Entrada    : PRÓXIMA RODADA")
    print("=" * 72 + "\n")
    return estrategia, cor_prevista


def carregar_historico_por_cursor(cur, ate_id=None):
    query = """
        SELECT id, rodada_id, color, cor, roll
        FROM blaze_historico
        WHERE status = 'complete'
          AND color IN (0, 1, 2)
          AND roll IS NOT NULL
    """
    params = []
    if ate_id is not None:
        query += " AND id <= %s"
        params.append(int(ate_id))
    query += " ORDER BY id ASC;"
    cur.execute(query, params)

    historico = []
    for db_id, rodada_id, color, cor_texto, roll in cur.fetchall():
        cor = obter_cor(cor_texto, color)
        if cor is None:
            continue
        try:
            roll = int(roll)
        except (TypeError, ValueError):
            continue
        historico.append({
            "id": int(db_id),
            "rodada_id": str(rodada_id),
            "cor": cor,
            "roll": roll,
        })
    return historico


def processar_novo_resultado(rodada_id, color, roll):
    """Processa uma rodada já persistida pelo collector."""
    if not init_engine_db():
        return None

    conn = get_db_connection()
    if not conn:
        return None

    try:
        rodada_id = str(rodada_id)
        color = int(color)
        roll = int(roll)
        cor_atual = cor_para_sigla(color)
        if cor_atual is None:
            print(f"⚠️ MOTOR: cor inválida na rodada {rodada_id}: {color}", flush=True)
            conn.close()
            return None

        now = datetime.now()
        with conn.cursor() as cur:
            rodada = _rodada_persistida(cur, rodada_id)
            if not rodada:
                raise RuntimeError(f"Rodada {rodada_id} não encontrada como complete.")
            rodada_db_id = int(rodada[0])

            cur.execute("""
                SELECT motor_ativo, ultima_rodada_processada
                FROM bot_estado
                WHERE id = 1;
            """)
            estado = cur.fetchone()
            motor_ativo = bool(estado[0]) if estado else False
            ultima_processada = str(estado[1]) if estado and estado[1] is not None else None

            # 1) Sempre reconcilia pendências que já possuem resultado.
            reconciliar_sinais_pendentes(cur, now)
            _resolver_pendente_da_rodada_atual(cur, rodada_db_id, now)

            # 2) Callback duplicado: não cria outro sinal.
            if ultima_processada == rodada_id:
                resumo = recalcular_bot_estado(cur, rodada_id, now)
                conn.commit()
                print(
                    f"♻️ MOTOR V8.5 | rodada {rodada_id} já processada; "
                    "reconciliação executada.",
                    flush=True,
                )
                return {
                    "ativo": motor_ativo,
                    "estrategia": resumo["estrategia"],
                    "cor": resumo["cor_sinal"],
                    "wins": resumo["wins"],
                    "losses": resumo["losses"],
                    "pendentes": resumo["pendentes"],
                    "profit": resumo["profit"],
                }

            # 3) Marca a rodada como processada mesmo com motor pausado.
            cur.execute("""
                UPDATE bot_estado
                SET ultima_rodada_processada = %s,
                    atualizado_em = %s
                WHERE id = 1;
            """, (rodada_id, now))

            # 4) Se ativo, a rodada atual fecha o contexto de 4 e pode gerar
            #    previsão para a PRÓXIMA. Não usamos nenhuma rodada futura.
            if motor_ativo:
                _criar_sinal_para_rodada_atual(cur, rodada_id, rodada_db_id)
            else:
                print(
                    f"⏸️ MOTOR V8.5 PAUSADO | rodada={rodada_id} | "
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
        }

    except Exception as e:
        print(f"❌ MOTOR V8.5: erro processando rodada {rodada_id}: {e}", flush=True)
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return None


def reconciliar_todos_sinais():
    """Reconciliação manual completa sem alterar o histórico das rodadas."""
    if not init_engine_db():
        return None
    conn = get_db_connection()
    if not conn:
        return None

    try:
        now = datetime.now()
        with conn.cursor() as cur:
            resolvidos = reconciliar_sinais_pendentes(cur, now, limite=50000)
            resumo = recalcular_bot_estado(cur, now=now)
        conn.commit()
        conn.close()
        print(
            "🧾 AUDITORIA V8.5 | "
            f"resolvidos={resolvidos} | pendentes={resumo['pendentes']} | "
            f"W={resumo['wins']} | L={resumo['losses']} | "
            f"profit={resumo['profit']:.2f}",
            flush=True,
        )
        return {"resolvidos": resolvidos, **resumo}
    except Exception as e:
        print(f"❌ AUDITORIA V8.5: erro: {e}", flush=True)
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
            reconciliar_sinais_pendentes(cur, datetime.now(), limite=5000)
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
            conn.rollback()
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
        print("✅ Motor V8.5 inicializado.")
        print("🟢 Estratégias ativas:")
        for i, (ctx, (pred, name)) in enumerate(REGRAS_V85.items(), 1):
            print(f"   {i}. {' → '.join(ctx)} → {pred} | {name}")
        print("\n🔄 Executando reconciliação inicial...")
        reconciliar_todos_sinais()
