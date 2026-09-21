import os
import requests
from datetime import datetime, timedelta

import sheets_db as db

# ================================================================
# V49.0 HIT & RUN + TELEGRAM + AUTO-STOP META +3 (SHEETS EDITION)
# ================================================================

APOSTA_BASE = 1.00
MAX_TENTATIVAS = 1
INVERSAO_ATIVA = False
META_DIARIA = 3

CORES = {0: "BRANCO", 1: "VERMELHO", 2: "PRETO"}
COR_SIGLA = {"BRANCO": "W", "VERMELHO": "R", "PRETO": "P"}

RESULTADO_PENDENTE = "PENDENTE"
RESULTADO_WIN = "WIN"
RESULTADO_LOSS = "LOSS"

_iniciado = False


# ================================================================
# TELEGRAM
# ================================================================
def enviar_telegram(mensagem):
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {"chat_id": chat_id, "text": mensagem, "parse_mode": "Markdown"}
    try:
        requests.post(url, data=data, timeout=5)
    except Exception:
        pass


# ================================================================
# FILTRO DE HORÁRIO E META (13h às 14h BRT)
# ================================================================
def get_hora_brt():
    return (datetime.utcnow() - timedelta(hours=3)).hour

def get_data_brt():
    return (datetime.utcnow() - timedelta(hours=3)).date()

def is_horario_permitido():
    return get_hora_brt() in [13, 14]


# ================================================================
# BANCO DE DADOS (GOOGLE PLANILHAS)
# ================================================================
def init_engine_db():
    global _iniciado
    ok = db.init_db()
    _iniciado = ok
    return ok


def cor_para_sigla(color):
    try:
        return COR_SIGLA.get(CORES.get(int(color)))
    except (TypeError, ValueError):
        return None


def get_lucro_dia():
    return db.lucro_dia()


def detectar_estrategia(hist):
    if not hist or len(hist) < 2:
        return None, None
    if not is_horario_permitido():
        return None, None
    if get_lucro_dia() >= META_DIARIA:
        return None, None

    cor_atual = hist[-1]["cor"]
    cor_anterior = hist[-2]["cor"]

    if cor_atual == "W" and cor_anterior in ["R", "P"]:
        cor_entrada = "P" if cor_anterior == "R" else "R"
        return cor_entrada, "V49.0 HIT & RUN | B -> Inversão"

    return None, None


def inverter_cor(cor):
    if cor == "R":
        return "P"
    if cor == "P":
        return "R"
    return cor


def _resultado_do_sinal(cor_entrada, cor_resultado):
    return RESULTADO_WIN if cor_resultado == cor_entrada else RESULTADO_LOSS


def _proxima_rodada_complete(base_db_id):
    return db.proxima_rodada_apos(base_db_id)


def resolver_sinal_por_rodada(sinal, rodada_resultado, cor_resultado, now):
    resultado = _resultado_do_sinal(sinal["cor_prevista"], cor_resultado)
    ok = db.resolver_sinal(sinal["id"], {
        "rodada_resultado": str(rodada_resultado),
        "cor_resultado": cor_resultado,
        "resultado": resultado,
        "resolvido_em": now,
    })
    if not ok:
        return False
    print(f"📊 SINAL RESOLVIDO | id={sinal['id']} | base={sinal['rodada_base']} | "
          f"resultado={rodada_resultado} | estratégia={sinal['estrategia']} | "
          f"entrada={sinal['cor_prevista']} | real={cor_resultado} | {resultado}",
          flush=True)
    return True


def _obter_ciclo():
    estado = db.ler_estado()
    return {
        "ativo": db.to_bool(estado.get("ciclo_ativo")),
        "id": db.txt(estado.get("ciclo_id")) or None,
        "tentativa": db.to_int(estado.get("tentativa_atual"), 0),
        "cor_regra": db.txt(estado.get("ciclo_cor_regra")) or None,
        "cor_entrada": db.txt(estado.get("ciclo_cor_entrada")) or None,
        "estrategia": db.txt(estado.get("ciclo_estrategia")) or None,
    }


def _finalizar_ciclo(now, motivo):
    db.atualizar_estado({
        "ciclo_ativo": False, "ciclo_id": "", "tentativa_atual": 0,
        "ciclo_cor_regra": "", "ciclo_cor_entrada": "", "ciclo_estrategia": "",
        "sinal_ativo": "", "cor_sinal": "", "rodada_base_sinal": "",
        "atualizado_em": now,
    })
    print(f"🏁 CICLO ENCERRADO | {motivo}", flush=True)

    lucro = get_lucro_dia()
    if "WIN" in motivo:
        msg = f"✅ *WIN!*\n💰 Lucro de hoje: {lucro:+}\n🎯 Meta: +{META_DIARIA}"
    else:
        msg = f"❌ *LOSS!*\n💰 Lucro de hoje: {lucro:+}\n🎯 Meta: +{META_DIARIA}"
    enviar_telegram(msg)

    if lucro >= META_DIARIA:
        enviar_telegram("🛑 *META DIÁRIA BATIDA!*\nO bot foi desligado e só volta a operar amanhã às 13h (Horário de Brasília).")


def _criar_tentativa(rodada_base, ciclo_id, tentativa, estrategia,
                     cor_regra, cor_entrada, now):
    db.inserir_sinal({
        "rodada_base": str(rodada_base),
        "estrategia": estrategia,
        "cor_prevista": cor_entrada,
        "resultado": "PENDENTE",
        "tentativa": int(tentativa),
        "ciclo_id": str(ciclo_id),
        "cor_regra": cor_regra,
        "cor_entrada": cor_entrada,
        "valor_aposta": APOSTA_BASE,
    })
    db.atualizar_estado({
        "ciclo_ativo": True,
        "ciclo_id": str(ciclo_id),
        "tentativa_atual": int(tentativa),
        "ciclo_cor_regra": cor_regra,
        "ciclo_cor_entrada": cor_entrada,
        "ciclo_estrategia": estrategia,
        "sinal_ativo": estrategia,
        "cor_sinal": cor_entrada,
        "rodada_base_sinal": str(rodada_base),
        "ultima_estrategia": estrategia,
        "atualizado_em": now,
    })

    nome_cor = "VERMELHO" if cor_entrada == "R" else "PRETO"
    print("\n" + "=" * 72)
    print(f"🎯 NOVA ENTRADA V49.0 | TENTATIVA {tentativa}/{MAX_TENTATIVAS} | ENTRADA NO {nome_cor}")
    print("=" * 72 + "\n")

    msg = f"🚨 *SINAL DETECTADO* 🚨\n\n👉 Entrada no: *{nome_cor}*\n💵 Valor: R$ {APOSTA_BASE:.2f}\n⏰ Janela: 13h - 14h"
    enviar_telegram(msg)


def _criar_primeiro_ciclo(rodada_id, rodada_db_id, now):
    historico = db.carregar_historico(ate_id=rodada_db_id)
    cor_regra, estrategia = detectar_estrategia(historico)
    if not estrategia:
        return False

    if db.existe_sinal_base(str(rodada_id)):
        return False

    ciclo_id = f"{rodada_id}-{int(datetime.now().timestamp() * 1000)}"
    cor_entrada = inverter_cor(cor_regra) if INVERSAO_ATIVA else cor_regra
    _criar_tentativa(rodada_id, ciclo_id, 1, estrategia, cor_regra, cor_entrada, now)
    return True


def _resolver_atual_e_avancar_ciclo(atual_id, now):
    pendentes = db.sinais_pendentes()
    if not pendentes:
        return

    hist = db.carregar_historico()
    pos_por_id = {h["id"]: i for i, h in enumerate(hist)}
    por_rodada = {h["rodada_id"]: h for h in hist}

    for sinal in pendentes:
        base = por_rodada.get(str(sinal["rodada_base"]))
        if not base:
            continue
        idx = pos_por_id.get(base["id"])
        if idx is None or idx + 1 >= len(hist):
            continue
        proxima = hist[idx + 1]
        if int(proxima["id"]) != int(atual_id):
            continue

        cor_resultado = proxima["cor"]
        if cor_resultado is None:
            continue
        if not resolver_sinal_por_rodada(sinal, proxima["rodada_id"], cor_resultado, now):
            continue

        resultado = _resultado_do_sinal(sinal["cor_prevista"], cor_resultado)
        ciclo = _obter_ciclo()
        if ciclo["ativo"] and ciclo["id"] == sinal["ciclo_id"]:
            if resultado == RESULTADO_WIN:
                _finalizar_ciclo(now, f"WIN na tentativa {sinal['tentativa']}/{MAX_TENTATIVAS}")
            elif int(sinal["tentativa"]) < MAX_TENTATIVAS:
                _criar_tentativa(proxima["rodada_id"], sinal["ciclo_id"],
                                 int(sinal["tentativa"]) + 1, sinal["estrategia"],
                                 sinal["cor_regra"], sinal["cor_entrada"], now)
            else:
                _finalizar_ciclo(now, "LOSS - Ciclo encerrado (Aposta Fixa)")


def reconciliar_sinais_pendentes(now, limite=5000):
    resolvidos = 0
    for sinal in db.sinais_pendentes(limite):
        base = db.buscar_rodada(sinal["rodada_base"])
        if not base:
            continue
        proxima = _proxima_rodada_complete(base["id"])
        if not proxima:
            continue
        cor_resultado = proxima["cor"]
        if cor_resultado is None:
            continue
        if resolver_sinal_por_rodada(sinal, proxima["rodada_id"], cor_resultado, now):
            resolvidos += 1
    return resolvidos


def recalcular_bot_estado(rodada_atual=None, now=None):
    est = db.estatisticas()
    estado = db.ler_estado()
    motor_ativo = db.to_bool(estado.get("motor_ativo"))
    ciclo = _obter_ciclo()

    rodada_base_sinal = ""
    if ciclo["ativo"] and ciclo["id"]:
        s = db.sinal_pendente_do_ciclo(ciclo["id"])
        if s:
            rodada_base_sinal = s["rodada_base"]

    now = now or db.agora()
    ultima = str(rodada_atual) if rodada_atual is not None else db.txt(estado.get("ultima_rodada_processada"))

    db.atualizar_estado({
        "wins": est["wins"],
        "losses": est["losses"],
        "whites": est["whites_internos"],
        "profit": round(est["profit"], 2),
        "sinal_ativo": ciclo["estrategia"] if ciclo["ativo"] else "",
        "cor_sinal": ciclo["cor_entrada"] if ciclo["ativo"] else "",
        "ultima_rodada_processada": ultima,
        "rodada_base_sinal": rodada_base_sinal,
        "ultima_estrategia": ciclo["estrategia"] if ciclo["ativo"] else "",
        "atualizado_em": now,
    })

    return {"wins": est["wins"], "losses": est["losses"], "pendentes": est["pendentes"],
            "whites_internos": est["whites_internos"], "profit": est["profit"],
            "sinal_ativo": ciclo["estrategia"] if ciclo["ativo"] else None,
            "cor_sinal": ciclo["cor_entrada"] if ciclo["ativo"] else None,
            "rodada_base_sinal": None,
            "estrategia": ciclo["estrategia"] if ciclo["ativo"] else None,
            "motor_ativo": motor_ativo, "ciclo_ativo": ciclo["ativo"],
            "ciclo_id": ciclo["id"], "tentativa_atual": ciclo["tentativa"],
            "ciclo_cor_regra": ciclo["cor_regra"], "ciclo_cor_entrada": ciclo["cor_entrada"]}


def processar_novo_resultado(rodada_id, color, roll):
    global _iniciado
    if not _iniciado:
        if not init_engine_db():
            return None
    try:
        rodada_id, color, roll = str(rodada_id), int(color), int(roll)
        cor_atual = cor_para_sigla(color)
        if cor_atual is None:
            print(f"⚠️ MOTOR: cor inválida na rodada {rodada_id}: {color}", flush=True)
            return None

        now = db.agora()
        rodada = db.buscar_rodada(rodada_id)
        if not rodada:
            print(f"⚠️ MOTOR: rodada {rodada_id} ainda não persistida como complete.", flush=True)
            return None
        rodada_db_id = int(rodada["id"])

        estado = db.ler_estado()
        motor_ativo = db.to_bool(estado.get("motor_ativo"))
        ultima_processada = db.txt(estado.get("ultima_rodada_processada")) or None

        if ultima_processada == rodada_id:
            resumo = recalcular_bot_estado(rodada_id, now)
            return {"ativo": motor_ativo, "estrategia": resumo["estrategia"],
                    "cor": resumo["cor_sinal"], "wins": resumo["wins"],
                    "losses": resumo["losses"], "pendentes": resumo["pendentes"],
                    "profit": resumo["profit"]}

        db.atualizar_estado({"ultima_rodada_processada": rodada_id, "atualizado_em": now})

        ciclo_antes = _obter_ciclo()
        if motor_ativo or ciclo_antes["ativo"]:
            _resolver_atual_e_avancar_ciclo(rodada_db_id, now)
            ciclo = _obter_ciclo()
            if motor_ativo and not ciclo["ativo"]:
                _criar_primeiro_ciclo(rodada_id, rodada_db_id, now)
        else:
            print(f"⏸️ MOTOR V49.0 PAUSADO | rodada={rodada_id} | coleta registrada, nenhuma nova previsão criada", flush=True)

        reconciliar_sinais_pendentes(now, limite=5000)
        resumo = recalcular_bot_estado(rodada_id, now)

        return {"ativo": motor_ativo, "estrategia": resumo["estrategia"],
                "cor": resumo["cor_sinal"], "wins": resumo["wins"],
                "losses": resumo["losses"], "pendentes": resumo["pendentes"],
                "profit": resumo["profit"]}

    except Exception as e:
        print(f"❌ MOTOR V49.0: erro processando rodada {rodada_id}: {e}", flush=True)
        return None


def reconciliar_todos_sinais():
    global _iniciado
    if not _iniciado:
        if not init_engine_db():
            return None
    try:
        now = db.agora()
        resolvidos = reconciliar_sinais_pendentes(now, limite=50000)
        resumo = recalcular_bot_estado(now=now)
        print(f"🧾 AUDITORIA V49.0 | resolvidos={resolvidos} | pendentes={resumo['pendentes']} | W={resumo['wins']} | L={resumo['losses']} | profit={resumo['profit']:.2f}", flush=True)
        return {"resolvidos": resolvidos, **resumo}
    except Exception as e:
        print(f"❌ AUDITORIA V49.0: erro: {e}", flush=True)
        return None


def obter_status_motor():
    global _iniciado
    if not _iniciado:
        if not init_engine_db():
            return {"ativo": False, "sinal": None, "cor": None, "estrategia": None,
                    "wins": 0, "losses": 0, "pendentes": 0, "profit": 0.0,
                    "ciclo_ativo": False, "tentativa_atual": 0}
    try:
        resumo = recalcular_bot_estado(now=db.agora())
        return {"ativo": resumo["motor_ativo"], "sinal": resumo["sinal_ativo"],
                "cor": resumo["cor_sinal"], "estrategia": resumo["estrategia"],
                "wins": resumo["wins"], "losses": resumo["losses"],
                "pendentes": resumo["pendentes"], "profit": resumo["profit"],
                "ciclo_ativo": resumo["ciclo_ativo"],
                "tentativa_atual": resumo["tentativa_atual"],
                "ciclo_cor_regra": resumo["ciclo_cor_regra"],
                "ciclo_cor_entrada": resumo["ciclo_cor_entrada"]}
    except Exception as e:
        print(f"❌ MOTOR: erro consultando status: {e}", flush=True)
        return {"ativo": False, "sinal": None, "cor": None, "estrategia": None,
                "wins": 0, "losses": 0, "pendentes": 0, "profit": 0.0,
                "ciclo_ativo": False, "tentativa_atual": 0}