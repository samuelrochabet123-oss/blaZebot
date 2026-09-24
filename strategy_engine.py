# =====================================================================
# MOTOR DE MARKOV EM TEMPO REAL (STRATEGY_ENGINE.PY)
# =====================================================================

from collections import defaultdict
from datetime import datetime, timedelta
import os
import requests
import sheets_db as db

APOSTA_BASE = 1.00
MAX_TENTATIVAS = 1
META_DIARIA = 3
STOP_LOSS = -3

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
# BANCO DE DADOS E INICIALIZAÇÃO
# ================================================================
def init_engine_db():
  global _iniciado
  ok = db.init_db()
  _iniciado = ok
  if ok:
    print("✅ strategy_engine (Markov) inicializado com sucesso.")
  return ok


def cor_para_sigla(color):
  try:
    return COR_SIGLA.get(CORES.get(int(color)))
  except (TypeError, ValueError):
    return None


def get_lucro_dia():
  return db.lucro_dia()


# ================================================================
# LÓGICA DE PREVISÃO DE MARKOV
# ================================================================
def detectar_estrategia_markov(hist):
  """Calcula as transições de Markov com base no histórico recente."""
  if not hist or len(hist) < 15:
    return None, None

  # Filtrar apenas Vermelho (R) e Preto (P) para a matriz
  dados_filtrados = [h for h in hist if h["cor"] in "RP"]
  if len(dados_filtrados) < 5:
    return None, None

  tamanho_padrao = 3
  transicoes = defaultdict(lambda: {"R": 0, "P": 0})

  for i in range(len(dados_filtrados) - tamanho_padrao):
    padrao = tuple(d["cor"] for d in dados_filtrados[i : i + tamanho_padrao])
    proxima_cor = dados_filtrados[i + tamanho_padrao]["cor"]
    transicoes[padrao][proxima_cor] += 1

  # Padrão atual (últimos 3 resultados)
  padrao_atual = tuple(d["cor"] for d in dados_filtrados[-tamanho_padrao:])
  estatisticas = transicoes[padrao_atual]
  total_amostras = estatisticas["R"] + estatisticas["P"]

  cor_entrada = None
  if total_amostras >= 2:
    if estatisticas["R"] > estatisticas["P"]:
      cor_entrada = "R"
    elif estatisticas["P"] > estatisticas["R"]:
      cor_entrada = "P"
    else:
      cor_entrada = dados_filtrados[-1]["cor"]
  else:
    # Fallback se não houver amostras suficientes para o padrão exato
    cor_entrada = "R" if dados_filtrados[-1]["cor"] == "P" else "P"

  nome_estrat = f"Markov (Padrão: {''.join(padrao_atual)})"
  return cor_entrada, nome_estrat


# ================================================================
# GERENCIAMENTO DE CICLOS E SINAIS
# ================================================================
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
      "ciclo_ativo": False,
      "ciclo_id": "",
      "tentativa_atual": 0,
      "ciclo_cor_regra": "",
      "ciclo_cor_entrada": "",
      "ciclo_estrategia": "",
      "sinal_ativo": "",
      "cor_sinal": "",
      "rodada_base_sinal": "",
      "atualizado_em": now,
  })
  print(f"🏁 CICLO DE MARKOV ENCERRADO | {motivo}", flush=True)

  lucro = get_lucro_dia()
  if "WIN" in motivo:
    msg = f"✅ *MARKOV WIN!*\n💰 Lucro do dia: {lucro:+.2f}\n🎯 Meta: +{META_DIARIA}"
  else:
    msg = f"❌ *MARKOV LOSS!*\n💰 Lucro do dia: {lucro:+.2f}\n🎯 Meta: +{META_DIARIA}"
  enviar_telegram(msg)


def _criar_tentativa(
    rodada_base, ciclo_id, tentativa, estrategia, cor_regra, cor_entrada, now
):
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
  print(f"🎯 SINAL MARKOV GERADO | Entrada no: {nome_cor}", flush=True)
  msg = f"🚨 *SINAL DE MARKOV* 🚨\n\n👉 Entrada no: *{nome_cor}*\n💵 Valor: R$ {APOSTA_BASE:.2f}"
  enviar_telegram(msg)


def _criar_primeiro_ciclo(rodada_id, rodada_db_id, now):
  historico = db.carregar_historico(ate_id=rodada_db_id)
  cor_entrada, estrategia = detectar_estrategia_markov(historico)
  if not estrategia or not cor_entrada:
    return False

  if db.existe_sinal_base(str(rodada_id)):
    return False

  ciclo_id = f"{rodada_id}-{int(datetime.now().timestamp() * 1000)}"
  _criar_tentativa(
      rodada_id, ciclo_id, 1, estrategia, cor_entrada, cor_entrada, now
  )
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
    if cor_resultado is None or cor_resultado == "W":
      continue  # Ignora Branco na resolução se necessário ou trata como loss

    resultado = (
        RESULTADO_WIN
        if cor_resultado == sinal["cor_prevista"]
        else RESULTADO_LOSS
    )
    ok = db.resolver_sinal(
        sinal["id"],
        {
            "rodada_resultado": str(proxima["rodada_id"]),
            "cor_resultado": cor_resultado,
            "resultado": resultado,
            "resolvido_em": now,
        },
    )
    if not ok:
      continue

    if resultado == RESULTADO_WIN:
      _finalizar_ciclo(now, "WIN de Markov")
    else:
      _finalizar_ciclo(now, "LOSS de Markov")


def reconciliar_sinais_pendentes(now, limite=5000):
  resolvidos = 0
  for sinal in db.sinais_pendentes(limite):
    base = db.buscar_rodada(sinal["rodada_base"])
    if not base:
      continue
    proxima = db.proxima_rodada_apos(base["id"])
    if not proxima:
      continue
    cor_resultado = proxima["cor"]
    if cor_resultado is None:
      continue

    resultado = (
        RESULTADO_WIN
        if cor_resultado == sinal["cor_prevista"]
        else RESULTADO_LOSS
    )
    if db.resolver_sinal(
        sinal["id"],
        {
            "rodada_resultado": str(proxima["rodada_id"]),
            "cor_resultado": cor_resultado,
            "resultado": resultado,
            "resolvido_em": now,
        },
    ):
      resolvidos += 1
  return resolvidos


def recalcular_bot_estado(rodada_atual=None, now=None):
  est = db.estatisticas()
  estado = db.ler_estado()
  motor_ativo = db.to_bool(estado.get("motor_ativo"))
  ciclo = _obter_ciclo()

  now = now or db.agora()
  ultima = (
      str(rodada_atual)
      if rodada_atual is not None
      else db.txt(estado.get("ultima_rodada_processada"))
  )

  db.atualizar_estado({
      "wins": est["wins"],
      "losses": est["losses"],
      "whites": est["whites_internos"],
      "profit": round(est["profit"], 2),
      "sinal_ativo": ciclo["estrategia"] if ciclo["ativo"] else "",
      "cor_sinal": ciclo["cor_entrada"] if ciclo["ativo"] else "",
      "ultima_rodada_processada": ultima,
      "atualizado_em": now,
  })

  return {
      "wins": est["wins"],
      "losses": est["losses"],
      "pendentes": est["pendentes"],
      "whites_internos": est["whites_internos"],
      "profit": est["profit"],
      "sinal_ativo": ciclo["estrategia"] if ciclo["ativo"] else None,
      "cor_sinal": ciclo["cor_entrada"] if ciclo["ativo"] else None,
      "motor_ativo": motor_ativo,
      "ciclo_ativo": ciclo["ativo"],
      "ciclo_id": ciclo["id"],
      "tentativa_atual": ciclo["tentativa"],
      "estrategia": ciclo["estrategia"] if ciclo["ativo"] else None,
  }


# ================================================================
# FUNÇÃO PRINCIPAL CHAMADA PELO COLETOR
# ================================================================
def processar_novo_resultado(rodada_id, color, roll):
  global _iniciado
  if not _iniciado:
    if not init_engine_db():
      return None
  try:
    rodada_id, color, roll = str(rodada_id), int(color), int(roll)
    cor_atual = cor_para_sigla(color)
    if cor_atual is None:
      return None

    now = db.agora()
    rodada = db.buscar_rodada(rodada_id)
    if not rodada:
      return None
    rodada_db_id = int(rodada["id"])

    estado = db.ler_estado()
    motor_ativo = db.to_bool(estado.get("motor_ativo"))
    ultima_processada = db.txt(estado.get("ultima_rodada_processada")) or None

    if ultima_processada == rodada_id:
      resumo = recalcular_bot_estado(rodada_id, now)
      return {
          "ativo": motor_ativo,
          "estrategia": resumo["estrategia"],
          "cor": resumo["cor_sinal"],
          "wins": resumo["wins"],
          "losses": resumo["losses"],
          "profit": resumo["profit"],
      }

    db.atualizar_estado(
        {"ultima_rodada_processada": rodada_id, "atualizado_em": now}
    )

    ciclo_antes = _obter_ciclo()
    if motor_ativo or ciclo_antes["ativo"]:
      _resolver_atual_e_avancar_ciclo(rodada_db_id, now)
      ciclo = _obter_ciclo()
      if motor_ativo and not ciclo["ativo"]:
        _criar_primeiro_ciclo(rodada_id, rodada_db_id, now)

    reconciliar_sinais_pendentes(now, limite=5000)
    resumo = recalcular_bot_estado(rodada_id, now)

    return {
        "ativo": motor_ativo,
        "estrategia": resumo["estrategia"],
        "cor": resumo["cor_sinal"],
        "wins": resumo["wins"],
        "losses": resumo["losses"],
        "profit": resumo["profit"],
    }
  except Exception as e:
    print(f"❌ Erro processando rodada no Markov: {e}", flush=True)
    return None


def reconciliar_todos_sinais():
  global _iniciado
  if not _iniciado:
    if not init_engine_db():
      return None
  now = db.agora()
  resolvidos = reconciliar_sinais_pendentes(now, limite=50000)
  resumo = recalcular_bot_estado(now=now)
  return {"resolvidos": resolvidos, **resumo}


def obter_status_motor():
  global _iniciado
  if not _iniciado:
    if not init_engine_db():
      return {
          "ativo": False,
          "sinal": None,
          "cor": None,
          "estrategia": None,
          "wins": 0,
          "losses": 0,
          "pendentes": 0,
          "profit": 0.0,
          "ciclo_ativo": False,
          "tentativa_atual": 0,
      }
  try:
    resumo = recalcular_bot_estado(now=db.agora())
    return {
        "ativo": resumo["motor_ativo"],
        "sinal": resumo["sinal_ativo"],
        "cor": resumo["cor_sinal"],
        "estrategia": resumo["estrategia"],
        "wins": resumo["wins"],
        "losses": resumo["losses"],
        "pendentes": resumo["pendentes"],
        "profit": resumo["profit"],
        "ciclo_ativo": resumo["ciclo_ativo"],
        "tentativa_atual": resumo["tentativa_atual"],
    }
  except Exception as e:
    print(f"❌ Erro consultando status do Markov: {e}", flush=True)
    return {
        "ativo": False,
        "sinal": None,
        "cor": None,
        "estrategia": None,
        "wins": 0,
        "losses": 0,
        "pendentes": 0,
        "profit": 0.0,
        "ciclo_ativo": False,
        "tentativa_atual": 0,
    }
