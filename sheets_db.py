# ================================================================
# sheets_db.py — BANCO DE DADOS EM GOOGLE PLANILHAS
# Substituto do PostgreSQL/Neon. Cada tabela = 1 aba.
# IMPORTANTE: IDs de linha são timestamps (ms) — ordem cronológica.
#
# Variáveis de ambiente:
#   GOOGLE_SHEETS_ID         -> ID da planilha (da URL)
#   GOOGLE_CREDENTIALS_JSON  -> CONTEÚDO do JSON da conta de serviço
#   (alternativa) GOOGLE_CREDENTIALS -> caminho do Secret File
# ================================================================

import json
import os
import threading
import time
from datetime import datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

PLANILHA_ID = os.getenv("GOOGLE_SHEETS_ID", "").strip()
ARQUIVO_CREDENCIAIS = "credenciais.json"

FMT = "%Y-%m-%d %H:%M:%S"
CACHE_TTL = 5  # segundos — protege a cota da API

HISTORICO = "blaze_historico"
ESTADO = "bot_estado"
SINAIS = "estrategia_sinais"
COLLECTOR = "collector_status"

CABECALHOS = {
    HISTORICO: ["id", "rodada_id", "color", "cor", "roll", "status",
                "room_id", "created_at", "updated_at", "coletado_em"],
    ESTADO: ["id", "motor_ativo", "sinal_ativo", "cor_sinal", "ultima_estrategia",
             "wins", "losses", "whites", "profit", "atualizado_em", "inicio_sessao",
             "ultima_rodada_processada", "rodada_base_sinal", "ciclo_ativo", "ciclo_id",
             "tentativa_atual", "ciclo_cor_regra", "ciclo_cor_entrada", "ciclo_estrategia"],
    SINAIS: ["id", "rodada_base", "estrategia", "cor_prevista", "rodada_resultado",
             "cor_resultado", "resultado", "criado_em", "resolvido_em", "tentativa",
             "ciclo_id", "cor_regra", "cor_entrada", "valor_aposta"],
    COLLECTOR: ["id", "conectado", "ultima_rodada", "ultimo_resultado_em",
                "total_ticks", "total_resultados", "total_duplicados",
                "total_erros_db", "atualizado_em"],
}

_lock = threading.RLock()
_planilha = None
_abas = {}
_cache = {}


# ================================================================
# CONVERSÃO DE TIPOS (Sheets guarda tudo como texto)
# ================================================================

def txt(v):
    return "" if v is None else str(v)

def to_int(v, padrao=None):
    try:
        return int(float(str(v).strip()))
    except Exception:
        return padrao

def to_float(v, padrao=None):
    try:
        return float(str(v).strip())
    except Exception:
        return padrao

def to_bool(v):
    return txt(v).strip().upper() in {"TRUE", "1", "1.0", "SIM", "YES", "VERDADEIRO"}

def parse_dt(v):
    v = txt(v).strip()
    if not v:
        return None
    try:
        return datetime.strptime(v[:19], FMT)
    except Exception:
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            return None

def agora():
    return datetime.utcnow().strftime(FMT)


# ================================================================
# CONEXÃO E CREDENCIAIS
# ================================================================

def credenciais_disponiveis():
    if (os.getenv("GOOGLE_CREDENTIALS_JSON") or "").strip():
        return True
    if (os.getenv("GOOGLE_CREDENTIALS") or "").strip():
        return True
    return os.path.exists(ARQUIVO_CREDENCIAIS)

def _credenciais():
    conteudo = (os.getenv("GOOGLE_CREDENTIALS_JSON") or "").strip()
    caminho_env = (os.getenv("GOOGLE_CREDENTIALS") or "").strip()

    # ── Diagnóstico (não expõe segredos) ──
    print("─── DIAGNÓSTICO CREDENCIAIS ───", flush=True)
    print(f"GOOGLE_CREDENTIALS_JSON definida: {bool(conteudo)}", flush=True)
    if conteudo:
        print(f"  tamanho: {len(conteudo)} caracteres", flush=True)
        print(f"  primeiros caracteres: {conteudo[:15]!r}", flush=True)
    print(f"GOOGLE_CREDENTIALS: {caminho_env or '(nao definida)'}", flush=True)
    if caminho_env:
        print(f"  arquivo existe: {os.path.exists(caminho_env)}", flush=True)
    print(f"credenciais.json local existe: {os.path.exists(ARQUIVO_CREDENCIAIS)}", flush=True)
    print("────────────────────────────────", flush=True)

    def abrir_arquivo(caminho):
        try:
            return Credentials.from_service_account_file(caminho, scopes=SCOPES)
        except json.JSONDecodeError:
            raise RuntimeError(
                f"O arquivo '{caminho}' existe mas NAO contem um JSON valido. "
                "Verifique o conteudo do Secret File no Render."
            )

    # 1) Conteudo JSON colado direto na variavel
    if conteudo:
        if conteudo.startswith("{"):
            try:
                info = json.loads(conteudo)
            except json.JSONDecodeError as e:
                raise RuntimeError(
                    f"GOOGLE_CREDENTIALS_JSON contem um JSON invalido ou truncado: {e}. "
                    "Abra o arquivo baixado do Google Cloud no Bloco de Notas, "
                    "selecione TUDO (Ctrl+A) e cole novamente."
                )
            return Credentials.from_service_account_info(info, scopes=SCOPES)
        # Veio um caminho em vez do conteudo
        if os.path.exists(conteudo):
            return abrir_arquivo(conteudo)
        raise RuntimeError(
            f"GOOGLE_CREDENTIALS_JSON nao e um JSON (comeca com {conteudo[:15]!r}) "
            "e tambem nao e um caminho existente. Ou cole o CONTEUDO do arquivo "
            "(deve comecar com { ), ou configure o Secret File."
        )

    # 2) Caminho via GOOGLE_CREDENTIALS (Secret File do Render)
    if caminho_env and os.path.exists(caminho_env):
        return abrir_arquivo(caminho_env)

    # 3) Arquivo local (para testes fora do Render)
    if os.path.exists(ARQUIVO_CREDENCIAIS):
        return abrir_arquivo(ARQUIVO_CREDENCIAIS)

    raise RuntimeError(
        "Nenhuma credencial Google encontrada. Use GOOGLE_CREDENTIALS_JSON "
        "(conteudo do JSON) ou Secret File + GOOGLE_CREDENTIALS=/etc/secrets/credenciais.json"
    )

def _conectar():
    global _planilha
    if _planilha:
        return _planilha
    if not PLANILHA_ID:
        raise RuntimeError("GOOGLE_SHEETS_ID não configurada no ambiente.")
    cliente = gspread.authorize(_credenciais())
    _planilha = cliente.open_by_key(PLANILHA_ID)
    return _planilha

def _aba(nome):
    with _lock:
        if nome in _abas:
            return _abas[nome]
        planilha = _conectar()
        try:
            aba = planilha.worksheet(nome)
        except gspread.WorksheetNotFound:
            aba = planilha.add_worksheet(
                title=nome, rows=5000, cols=len(CABECALHOS[nome]) + 5
            )
        if not aba.row_values(1):
            aba.update(values=[CABECALHOS[nome]], range_name="A1",
                       value_input_option="RAW")
        _abas[nome] = aba
        return aba


# ================================================================
# CACHE DE LEITURA
# ================================================================

def _invalidar(nome):
    _cache.pop(nome, None)

def registros(nome, force=False):
    with _lock:
        hit = _cache.get(nome)
        agora_ts = time.time()
        if not force and hit and agora_ts - hit[0] < CACHE_TTL:
            return hit[1]
        aba = _aba(nome)
        dados = aba.get_all_records(numericise_ignore=["all"], default_blank="")
        _cache[nome] = (agora_ts, dados)
        return dados

def _linha(nome, reg):
    valores = []
    for col in CABECALHOS[nome]:
        v = reg.get(col, "")
        if v is None:
            v = ""
        elif isinstance(v, bool):
            v = "TRUE" if v else "FALSE"
        elif isinstance(v, datetime):
            v = v.strftime(FMT)
        valores.append(str(v))
    return valores


# ================================================================
# INICIALIZAÇÃO
# ================================================================

def _garantir_linha(nome, defaults):
    with _lock:
        for r in registros(nome, force=True):
            if to_int(r.get("id")) == 1:
                return
        reg = {"id": 1}
        reg.update(defaults)
        reg["atualizado_em"] = agora()
        _aba(nome).append_row(_linha(nome, reg), value_input_option="RAW")
        _invalidar(nome)

def init_db():
    try:
        for nome in CABECALHOS:
            _aba(nome)
        _garantir_linha(ESTADO, {
            "motor_ativo": False, "wins": 0, "losses": 0, "whites": 0,
            "profit": 0.0, "tentativa_atual": 0, "ciclo_ativo": False,
        })
        _garantir_linha(COLLECTOR, {
            "conectado": False, "total_ticks": 0, "total_resultados": 0,
            "total_duplicados": 0, "total_erros_db": 0,
        })
        print(f"✅ Google Planilhas OK ({_conectar().title})", flush=True)
        return True
    except Exception as e:
        print(f"❌ Erro inicializando Google Planilhas: {e}", flush=True)
        return False


# ================================================================
# blaze_historico
# ================================================================

def inserir_rodada(rodada_id, color, cor_nome, roll, status, room_id,
                   created_at, updated_at):
    """Insere rodada. Retorna novo id ou None se duplicada."""
    with _lock:
        rid = str(rodada_id)
        for r in registros(HISTORICO):
            if txt(r.get("rodada_id")) == rid:
                return None
        novo_id = str(int(time.time() * 1000))
        reg = {
            "id": novo_id, "rodada_id": rid, "color": color, "cor": cor_nome,
            "roll": roll, "status": status, "room_id": room_id,
            "created_at": created_at or "", "updated_at": updated_at or "",
            "coletado_em": agora(),
        }
        _aba(HISTORICO).append_row(_linha(HISTORICO, reg), value_input_option="RAW")
        _invalidar(HISTORICO)
        return novo_id

def buscar_rodada(rodada_id):
    rid = str(rodada_id)
    for r in registros(HISTORICO):
        if txt(r.get("rodada_id")) == rid and txt(r.get("status")) == "complete":
            return {
                "id": to_int(r.get("id")),
                "rodada_id": rid,
                "color": to_int(r.get("color")),
                "cor": txt(r.get("cor")),
                "roll": to_int(r.get("roll")),
            }
    return None

def carregar_historico(ate_id=None):
    hist = []
    for r in registros(HISTORICO):
        if txt(r.get("status")) != "complete":
            continue
        color = to_int(r.get("color"))
        if color not in (0, 1, 2):
            continue
        roll = to_int(r.get("roll"))
        if roll is None:
            continue
        i = to_int(r.get("id"))
        if i is None:
            continue
        if ate_id is not None and i > ate_id:
            continue
        cor = cor_de(txt(r.get("cor")), color)
        if cor is None:
            continue
        hist.append({"id": i, "rodada_id": txt(r.get("rodada_id")),
                     "color": color, "cor": cor, "roll": roll})
    hist.sort(key=lambda h: h["id"])
    return hist

def proxima_rodada_apos(base_id):
    for h in carregar_historico():
        if h["id"] > int(base_id):
            return h
    return None

def rodadas_da_sessao(inicio, limite=24):
    rows = []
    for r in registros(HISTORICO):
        if txt(r.get("status")) != "complete":
            continue
        coletado = txt(r.get("coletado_em"))
        if inicio and (not coletado or coletado < inicio):
            continue
        rows.append({
            "id": to_int(r.get("id")),
            "roll": to_int(r.get("roll")),
            "color": to_int(r.get("color")),
            "cor": txt(r.get("cor")),
            "rodada_id": txt(r.get("rodada_id")),
        })
    rows.sort(key=lambda x: x["id"], reverse=True)
    return rows[:limite]


# ================================================================
# bot_estado / collector_status (linha única id = 1)
# ================================================================

def _atualizar_unica(nome, campos):
    with _lock:
        dados = registros(nome, force=True)
        for i, r in enumerate(dados):
            if to_int(r.get("id")) == 1:
                reg = dict(r)
                reg.update(campos)
                reg["atualizado_em"] = agora()
                _aba(nome).update(values=[_linha(nome, reg)],
                                  range_name=f"A{i + 2}",
                                  value_input_option="RAW")
                _invalidar(nome)
                return True
        reg = {"id": 1}
        reg.update(campos)
        reg["atualizado_em"] = agora()
        _aba(nome).append_row(_linha(nome, reg), value_input_option="RAW")
        _invalidar(nome)
        return True

def ler_estado():
    for r in registros(ESTADO):
        if to_int(r.get("id")) == 1:
            return dict(r)
    return {}

def atualizar_estado(campos):
    return _atualizar_unica(ESTADO, campos)

def ler_collector():
    for r in registros(COLLECTOR):
        if to_int(r.get("id")) == 1:
            return dict(r)
    return {}

def atualizar_collector(campos):
    return _atualizar_unica(COLLECTOR, campos)


# ================================================================
# estrategia_sinais
# ================================================================

def _normalizar_sinal(r):
    return {
        "id": to_int(r.get("id")),
        "rodada_base": txt(r.get("rodada_base")),
        "estrategia": txt(r.get("estrategia")),
        "cor_prevista": txt(r.get("cor_prevista")),
        "rodada_resultado": txt(r.get("rodada_resultado")) or None,
        "cor_resultado": txt(r.get("cor_resultado")) or None,
        "resultado": txt(r.get("resultado")),
        "criado_em": txt(r.get("criado_em")) or None,
        "resolvido_em": txt(r.get("resolvido_em")) or None,
        "tentativa": to_int(r.get("tentativa"), 1),
        "ciclo_id": txt(r.get("ciclo_id")) or None,
        "cor_regra": txt(r.get("cor_regra")) or None,
        "cor_entrada": txt(r.get("cor_entrada")) or None,
        "valor_aposta": to_float(r.get("valor_aposta"), 1.0),
    }

def sinais_todos():
    out = [_normalizar_sinal(r) for r in registros(SINAIS)]
    out.sort(key=lambda s: s["id"] or 0)
    return out

def sinais_pendentes(limite=None):
    pend = [s for s in sinais_todos() if txt(s["resultado"]).upper() == "PENDENTE"]
    return pend[:limite] if limite else pend

def existe_sinal_base(rodada_base):
    rb = str(rodada_base)
    return any(s["rodada_base"] == rb for s in sinais_todos())

def inserir_sinal(dados):
    with _lock:
        reg = {
            "id": str(int(time.time() * 1000)), "rodada_base": "",
            "estrategia": "", "cor_prevista": "", "rodada_resultado": "",
            "cor_resultado": "", "resultado": "PENDENTE",
            "criado_em": agora(), "resolvido_em": "", "tentativa": 1,
            "ciclo_id": "", "cor_regra": "", "cor_entrada": "",
            "valor_aposta": 1.0,
        }
        reg.update(dados)
        _aba(SINAIS).append_row(_linha(SINAIS, reg), value_input_option="RAW")
        _invalidar(SINAIS)
        return reg["id"]

def resolver_sinal(sinal_id, campos):
    """Resolve somente se ainda estiver PENDENTE. Retorna False caso contrário."""
    with _lock:
        aba = _aba(SINAIS)
        dados = aba.get_all_records(numericise_ignore=["all"], default_blank="")
        for i, r in enumerate(dados):
            if txt(r.get("id")) == str(sinal_id):
                if txt(r.get("resultado")).upper() != "PENDENTE":
                    return False
                reg = dict(r)
                reg.update(campos)
                aba.update(values=[_linha(SINAIS, reg)],
                           range_name=f"A{i + 2}", value_input_option="RAW")
                _invalidar(SINAIS)
                return True
        return False

def sinal_pendente_do_ciclo(ciclo_id):
    cid = str(ciclo_id)
    pend = [s for s in sinais_pendentes() if s["ciclo_id"] == cid]
    return pend[-1] if pend else None

def estatisticas():
    wins = losses = pendentes = whites_internos = 0
    profit = 0.0
    for s in sinais_todos():
        res = txt(s["resultado"]).upper()
        if res == "WIN":
            wins += 1
            profit += s["valor_aposta"] or 0.0
        elif res == "LOSS":
            losses += 1
            profit -= s["valor_aposta"] or 0.0
            if s["cor_resultado"] == "W":
                whites_internos += 1
        elif res == "PENDENTE":
            pendentes += 1
    return {"wins": wins, "losses": losses, "pendentes": pendentes,
            "whites_internos": whites_internos, "profit": profit}

def lucro_dia():
    hoje = (datetime.utcnow() - timedelta(hours=3)).date()
    total = 0
    for s in sinais_todos():
        d = parse_dt(s.get("criado_em"))
        if not d or (d - timedelta(hours=3)).date() != hoje:
            continue
        res = txt(s["resultado"]).upper()
        if res == "WIN":
            total += 1
        elif res == "LOSS":
            total -= 1
    return total


# ================================================================
# COR NORMALIZADA (R / P / W)
# ================================================================

def cor_de(cor_texto, color):
    t = txt(cor_texto).upper().strip()
    if "VERMELHO" in t or t in {"R", "RED", "V", "VI"}:
        return "R"
    if "PRETO" in t or t in {"P", "BLACK", "B"}:
        return "P"
    if "BRANCO" in t or t in {"W", "WHITE"}:
        return "W"
    if color == 0:
        return "W"
    if color == 1:
        return "R"
    if color == 2:
        return "P"
    return None
