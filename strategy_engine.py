import math
import os
import requests
from datetime import datetime, timedelta

import sheets_db as db

# ================================================================
# MOTOR CONSOLIDADO DE Z-SCORE + FILTRO DE HORÁRIO (BRASÍLIA)
# ================================================================

APOSTA_BASE = 1.00
MAX_TENTATIVAS = 1
META_DIARIA = 3

CORES = {0: "BRANCO", 1: "VERMELHO", 2: "PRETO"}
COR_SIGLA = {"BRANCO": "W", "VERMELHO": "R", "PRETO": "P"}

RESULTADO_PENDENTE = "PENDENTE"
RESULTADO_WIN = "WIN"
RESULTADO_LOSS = "LOSS"

_iniciado = False

# Horários de Brasília permitidos com base na auditoria de alta performance
HORARIOS_PERMITIDOS = {1, 4, 8, 9, 10, 12, 16, 17, 18, 19}

# Mapeamento das 3 regras unificadas: (Z-alvo, Cor de Aposta)
REGRAS_CONFIG = {
    1.10: "R",  # 1ª Regra: Z=1.10 x3 + Quebra -> Aposta em Vermelho
    0.73: "P",  # 2ª Regra: Z=0.73 x3 + Quebra -> Aposta em Preto
    0.37: "P",  # 3ª Regra: Z=0.37 x3 + Quebra -> Aposta em Preto
}


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
# FILTRO DE HORÁRIO E META (Horário de Brasília)
# ================================================================
def get_hora_brt():
  return (datetime.utcnow() - timedelta(hours=3)).hour


def get_data_brt():
  return (datetime.utcnow() - timedelta(hours=3)).date()


def is_horario_permitido():
  return get_hora_brt() in HORARIOS_PERMITIDOS


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


def calcular_z_scores(cores_lista, tamanho_janela=30):
  historico_z = []
  i = tamanho_janela
  while i < len(cores_lista):
    janela = cores_lista[i - tamanho_janela : i]
    qtd_r = janela.count("R")
    qtd_p = janela.count("P")
    total = qtd_r + qtd_p
    if total == 0:
      i += 1
      continue

    media = total / 2.0
    desvio_padrao = math.sqrt(total * 0.5 * 0.5)
    maior_qtd = max(qtd_r, qtd_p)

    z_score = (
        round((maior_qtd - media) / desvio_padrao, 2)
        if desvio_padrao > 0
        else 0.0
    )
    historico_z.append(z_score)
    i += 1
  return historico_z


def detectar_estrategia(hist):
  if not hist or len(hist) < 35:
    return None, None
  if not is_horario_permitido():
    return None, None
  if get_lucro_dia() >= META_DIARIA:
    return None, None

  # Filtra apenas R e P para o cálculo de Z-Score
  cores = [h["cor"] for h in hist if h["cor"] in "RP"]
  if len(cores) < 33:
    return None, None

  z_scores = calcular_z_scores(cores, tamanho_janela=30)
  if len(z_scores) < 4:
    return None, None

  # Últimos 4 valores de Z-Score calculados
  z1 = z_scores[-4]
  z2 = z_scores[-3]
  z3 = z_scores[-2]
  z_quebra = z_scores[-1]

  # Valida a regra unificada: 3 repetições de Z seguidas de uma quebra diferente
  if z1 == z2 == z3 and z1 in REGRAS_CONFIG and z_quebra != z1:
    cor_entrada = REGRAS_CONFIG[z1]
    nome_estrategia = f"Z-Score Unificado (Z={z1})"
    return cor_entrada, nome_estrategia

  return None, None


def _resultado_do_sinal(cor_entrada, cor_resultado):
  return RESULTADO_WIN if cor_resultado == cor_entrada else RESULTADO_LOSS


def _proxima_rodada_complete(base_db_id):
  return db.proxima_rodada_apos(base_db_id)


def resolver_sinal_por_rodada(sinal, rodada_resultado, cor_resultado, now):
  resultado = _resultado_do_sinal(sinal["cor_prevista"], cor_resultado)
  ok = db.resolver_sinal(
      sinal["id"],
      {
          "rodada_resultado": str(rodada_resultado),
          "cor_resultado": cor_resultado,
          "resultado": resultado,
          "resolvido_em": now,
      },
  )
  if not ok:
    return False
  print(
      f"📊 SINAL RESOLVIDO | id={sinal['id']} | base={sinal['rodada_base']} |"
      f" resultado={rodada_resultado} | estratégia={sinal['estrategia']} |"
      f" entrada={sinal['cor_prevista']} | real={cor_resultado} | {resultado}",
      flush=True,
  )
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
  print(f"🏁 CICLO ENCERRADO | {motivo}", flush=True)

  lucro = get_lucro_dia()
  if "WIN" in motivo:
    msg = f"✅ *WIN!*\n💰 Lucro de hoje: {lucro:+}\n🎯 Meta: +{META_DIARIA}"
  else:
    msg = f"❌ *LOSS!*\n💰 Lucro de hoje: {lucro:+}\n🎯 Meta: +{META_DIARIA}"
  enviar_telegram(msg)

  if lucro >= META_DIARIA:
    enviar_telegram(
        "🛑 *META DIÁRIA BATIDA!*\nO bot foi desligado e só volta a operar nos"
        " horários programados."
    )


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
      "cor_regra": str(cor_regra),
      "cor_entrada": cor_entrada,
      "valor_aposta": APOSTA_BASE,
  })
  db.atualizar_estado({
      "ciclo_ativo": True,
      "ciclo_id": str(ciclo_id),
      "tentativa_atual": int(tentativa),
      "ciclo_cor_regra": str(cor_regra),
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
  print(
      f"🎯 NOVO SINAL Z-SCORE | TENTATIVA {tentativa}/{MAX_TENTATIVAS} |"
      f" ENTRADA NO {nome_cor}"
  )
  print("=" * 72 + "\n")

  msg = (
      f"🚨 *SINAL Z-SCORE UNIFICADO* 🚨\n\n👉 Entrada no: *{nome_cor}*\n💵 Valor:"
      f" R$ {APOSTA_BASE:.2f}\n⏰ Hora BRT: {get_hora_brt():02d}:00"
  )
  enviar_telegram(msg)


def _criar_primeiro_ciclo(rodada_id, rodada_db_id, now):
  historico = db.carregar_historico(ate_id=rodada_db_id)
  cor_entrada, estrategia = detectar_estrategia(historico)
  if not estrategia:
    return False

  if db.existe_sinal_base(str(rodada_id)):
    return False

  ciclo_id = f"{rodada_id}-{int(datetime.now().timestamp() * 1000)}"
  _criar_tentativa(
      rodada_id, ciclo_id, 1, estrategia, "Z-Score", cor_entrada, now
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
    if cor_resultado is None:
      continue
    if not resolver_sinal_por_rodada(
        sinal, proxima["rodada_id"], cor_resultado, now
    ):
      continue

    resultado = _resultado_do_sinal(sinal["cor_prevista"], cor_resultado)
    ciclo = _obter_ciclo()
    if ciclo["ativo"] and ciclo["id"] == sinal["ciclo_id"]:
      if resultado == RESULTADO_WIN:
        _finalizar_ciclo(
            now, f"WIN na tentativa {sinal['tentativa']}/{MAX_TENTATIVAS}"
        )
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
    if resolver_sinal_por_rodada(
        sinal, proxima["rodada_id"], cor_resultado, now
    ):
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
      "rodada_base_sinal": rodada_base_sinal,
      "ultima_estrategia": ciclo["estrategia"] if ciclo["ativo"] else "",
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
      "rodada_base_sinal": None,
      "estrategia": ciclo["estrategia"] if ciclo["ativo"] else None,
      "motor_ativo": motor_ativo,
      "ciclo_ativo": ciclo["ativo"],
      "ciclo_id": ciclo["id"],
      "tentativa_atual": ciclo["tentativa"],
      "ciclo_cor_regra": ciclo["ciclo_cor_regra"],
      "ciclo_cor_entrada": ciclo["ciclo_cor_entrada"],
  }


def processar_novo_resultado(rodada_id, color, roll):
  global _iniciado
  if not _iniciado:
    if not init_engine_db():
      return None
  try:
    rodada_id, color, roll = str(rodada_id), int(color), int(roll)
    cor_atual = cor_para_sigla(color)
    if cor_atual is None:
      print(
          f"⚠️ MOTOR: cor inválida na rodada {rodada_id}: {color}", flush=True
      )
      return None

    now = db.agora()
    rodada = db.buscar_rodada(rodada_id)
    if not rodada:
      print(
          f"⚠️ MOTOR: rodada {rodada_id} ainda não persistida como complete.",
          flush=True,
      )
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
          "pendentes": resumo["pendentes"],
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
    else:
      print(
          f"⏸️ MOTOR Z-SCORE PAUSADO | rodada={rodada_id} | fora da janela ou"
          " pausado",
          flush=True,
      )

    reconciliar_sinais_pendentes(now, limite=5000)
    resumo = recalcular_bot_estado(rodada_id, now)

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
    print(
        f"❌ MOTOR Z-SCORE: erro processando rodada {rodada_id}: {e}",
        flush=True,
    )
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
    print(
        f"🧾 AUDITORIA Z-SCORE | resolvidos={resolvidos} |"
        f" pendentes={resumo['pendentes']} | W={resumo['wins']} |"
        f" L={resumo['losses']} | profit={resumo['profit']:.2f}",
        flush=True,
    )
    return {"resolvidos": resolvidos, **resumo}
  except Exception as e:
    print(f"❌ AUDITORIA Z-SCORE: erro: {e}", flush=True)
    return None


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
        "ciclo_cor_regra": resumo["ciclo_cor_regra"],
        "ciclo_cor_entrada": resumo["ciclo_cor_entrada"],
    }
  except Exception as e:
    print(f"❌ MOTOR: erro consultando status: {e}", flush=True)
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
