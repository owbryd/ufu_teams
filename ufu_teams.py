import os, re, sys, json, time, base64, hashlib, urllib.parse, atexit, signal
from pathlib import Path
from datetime import datetime
import requests

BASE_SP = "https://ufubr.sharepoint.com"
pasta_base = Path(__file__).parent / "UFU_Teams"
arquivo_ctrl = pasta_base / ".baixados.json"
EXTENSOES = {".pdf", ".pptx", ".ppt", ".docx", ".doc", ".xlsx", ".zip", ".txt"}
intervalo_watch = 300  # segundos entre verificações
margem_renovar = 10  # renova token X minutos antes de expirar
debug = False


_DIR = Path(__file__).parent
perfil_dir_padrao = _DIR / "ufu_perfil"
arquivo_token_padrao = _DIR / "token.txt"

# capturar token
_URL_ONEDRIVE = "https://ufubr-my.sharepoint.com/"
_URL_SPAPPBAR = "https://ufubr.sharepoint.com/_layouts/15/spappbar.aspx?workload=files"
_URL_SP_MAIN = "https://ufubr.sharepoint.com/_layouts/15/sharepoint.aspx"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
_PADROES_TOKEN = [
    r"ufubr-my\.sharepoint\.com/personal/.*/_api/",
    r"ufubr\.sharepoint\.com/.*/_api/",
]


minhas_turmas: list[str] = []


# tg
telegram_ativo = True
telegram_token = os.environ.get(
    "TELEGRAM_TOKEN", ""
)
telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
telegram_tamanho_max = 49 * 1024 * 1024  # 49 MB — limite de envio

# zap
whatsapp_ativo = True
whatsapp_numero = os.environ.get("WHATSAPP_NUMERO", "")
whatsapp_servidor = os.environ.get("WHATSAPP_SERVIDOR", "http://localhost:3737")

BIBLIOTECAS_AULA = {
    "Material de Aula",
    "Class Files",
    "Materiais",
    "Materials",
    "Slides",
    "Aulas",
}
PASTAS_ALUNOS = {
    "Student Work",
    "Trabalhos dos Alunos",
    "Trabalho dos Alunos",
    "Entregas",
    "Submissions",
}
LISTAS_SISTEMA = {
    "SiteAssets",
    "Ativos do Site",
    "Style Library",
    "Biblioteca de Estilos",
    "FormServerTemplates",
    "Modelos de Formulário",
    "Preservation Hold Library",
    "Site Collection Documents",
    "Site Collection Images",
}


# jwt
def _jwt_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _segundos_para_expirar(token: str) -> int:
    try:
        exp = int(_jwt_claims(token).get("exp", 0))
        return int(exp - time.time()) if exp else -1
    except (ValueError, TypeError):
        return -1


def _token_valido(token: str) -> bool:
    return bool(token) and _segundos_para_expirar(token) > margem_renovar * 60


def _fmt_tempo(s: int) -> str:
    if s <= 0:
        return "expirado"
    h, m = divmod(s // 60, 60)
    return f"{h}h {m}min" if h else f"{m}min"


def _erro_playwright():
    print("sem playwright")
    print("       pip install playwright && playwright install chromium")
    sys.exit(1)


def fazer_login(perfil_dir: Path = perfil_dir_padrao) -> bool:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        _erro_playwright()

    perfil_dir.mkdir(parents=True, exist_ok=True)
    print("abrindo browser para login...")
    print(f"perfil será salvo em: {perfil_dir.resolve()}\n")
    print("faça login no browser que abriu")

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(perfil_dir),
            headless=False,
            user_agent=_USER_AGENT,
            args=["--start-maximized"],
            no_viewport=True,
        )
        page = ctx.new_page()
        page.goto(_URL_ONEDRIVE, timeout=60_000)

        dominios_login = (
            "login.microsoftonline.com",
            "login.microsoft.com",
            "login.live.com",
            "account.activedirectory.windowsazure.com",
        )
        tempo_max_s = 600
        logado = False
        for _ in range(tempo_max_s):
            page.wait_for_timeout(1_000)
            url_atual = page.url
            if "ufubr-my.sharepoint.com" in url_atual and not any(
                d in url_atual for d in dominios_login
            ):
                logado = True
                break

        if logado:
            page.wait_for_timeout(2_000)
            print("login detectado, salvando perfil...")
        else:
            print("tempo esgotado")

        ctx.close()

    if not logado:
        return False

    print("perfil salvo.\n")
    return True


def capturar_token(
    perfil_dir: Path = perfil_dir_padrao, headless: bool = True
) -> str | None:
    # pegar jwt do perfil salvo
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        _erro_playwright()

    if not perfil_dir.exists():
        print(f"perfil não encontrado em '{perfil_dir}'.")
        print("       delete a pasta do perfil e rode novamente para refazer o login.")
        return None

    token_ref = [None]

    def interceptar(request):
        if token_ref[0]:
            return
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return
        for padrao in _PADROES_TOKEN:
            if re.search(padrao, request.url, re.IGNORECASE):
                token_ref[0] = auth.split(" ", 1)[1].strip()
                print("token capturado")
                break

    print("capturando token...")
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(perfil_dir),
            headless=headless,
            user_agent=_USER_AGENT,
        )
        page = ctx.new_page()
        page.on("request", interceptar)

        def _aguardar(s):
            for _ in range(s):
                page.wait_for_timeout(1_000)
                if token_ref[0]:
                    return True
            return False

        try:
            page.goto(_URL_ONEDRIVE, wait_until="domcontentloaded", timeout=30_000)

            if any(
                d in page.url
                for d in ("login.microsoftonline.com", "login.microsoft.com")
            ):
                print("sessão do browser expirou")
                ctx.close()
                return None

            if _aguardar(15):
                return token_ref[0]

            page.goto(_URL_SPAPPBAR, wait_until="domcontentloaded", timeout=30_000)
            if _aguardar(10):
                return token_ref[0]

            page.goto(_URL_SP_MAIN, wait_until="domcontentloaded", timeout=30_000)
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except PWTimeout:
                pass
            if _aguardar(10):
                return token_ref[0]

            page.goto(_URL_ONEDRIVE, wait_until="domcontentloaded", timeout=30_000)
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except PWTimeout:
                pass
            _aguardar(10)

        except PWTimeout:
            print("[timeout")
        except Exception as e:
            print(f"captura: {e}")
        finally:
            ctx.close()

    return token_ref[0]


def obter_token_valido(
    perfil_dir: Path,
    arquivo_token: Path,
    headless: bool = True,
) -> str | None:

    # token salvo em arquivo
    if arquivo_token.exists():
        salvo = arquivo_token.read_text(encoding="utf-8").strip()
        if salvo and _token_valido(salvo):
            return salvo
        elif salvo:
            print("token salvo expirou, capturando novo...")

    # via playwright
    novo = capturar_token(perfil_dir, headless)
    if novo:
        arquivo_token.parent.mkdir(parents=True, exist_ok=True)
        arquivo_token.write_text(novo, encoding="utf-8")
        print("token salvo")
    return novo


_flag_401 = False


def hdrs(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json;odata=nometadata",
    }


def get(url, token, *, timeout=15, stream=False):
    global _flag_401
    r = requests.get(url, headers=hdrs(token), timeout=timeout, stream=stream)
    if r.status_code == 401:
        _flag_401 = True
    return r


def sp_cells(row):
    cells = row.get("Cells", [])
    if isinstance(cells, dict):
        cells = cells.get("results", [])
    return {c["Key"]: c["Value"] for c in cells if "Key" in c}


def sp_rows(dados):
    rows = (
        dados.get("PrimaryQueryResult", {})
        .get("RelevantResults", {})
        .get("Table", {})
        .get("Rows", [])
    )
    return rows.get("results", []) if isinstance(rows, dict) else (rows or [])


def extrair_slug(url):
    m = re.search(r"/sites/([^/?#]+)", url)
    return m.group(1) if m else url.split("/")[-1]


def carregar_controle():
    if arquivo_ctrl.exists():
        try:
            return json.loads(arquivo_ctrl.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def salvar_controle(ctrl):
    arquivo_ctrl.parent.mkdir(parents=True, exist_ok=True)
    arquivo_ctrl.write_text(
        json.dumps(ctrl, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# turmas
def _tem_biblioteca_aula(token, slug):
    url = (
        f"{BASE_SP}/sites/{slug}/_api/web/lists"
        f"?$filter=BaseTemplate eq 101 and Hidden eq false&$select=Title"
    )
    try:
        r = get(url, token, timeout=8)
        listas = r.json().get("value", []) if r.status_code == 200 else []
        if isinstance(listas, dict):
            listas = listas.get("results", [])
        return bool({l.get("Title") for l in listas} & BIBLIOTECAS_AULA)
    except Exception:
        return False


def listar_turmas(token):
    print("buscando turmas...")
    encontrados, vistos = [], set()

    def adicionar(nome, slug):
        if slug and slug not in vistos:
            vistos.add(slug)
            encontrados.append(
                {"nome": nome, "url": f"{BASE_SP}/sites/{slug}", "slug": slug}
            )

    def e_turma(slug):
        return "grupoufubr" in slug.lower() or "grupoufu" in slug.lower()

    try:
        url = (
            f"{BASE_SP}/_api/search/query"
            f"?querytext='contentclass:STS_Site path:{BASE_SP}/sites'"
            f"&rowlimit=100&selectproperties='Title,Path'&Properties='EnableDynamicGroups:true'"
        )
        r = get(url, token)
        if r.status_code == 200:
            for row in sp_rows(r.json()):
                cells = sp_cells(row)
                slug = extrair_slug(cells.get("Path", ""))
                if slug and e_turma(slug) and _tem_biblioteca_aula(token, slug):
                    adicionar(cells.get("Title", slug), slug)
                    if debug:
                        print(f"  [ok] SP search: {slug}")
    except Exception as e:
        if debug:
            print(f"  [aviso] Search sites: {e}")

    # manual
    if minhas_turmas:
        encontrados = [s for s in encontrados if s["slug"] in minhas_turmas]
        vistos_já = {s["slug"] for s in encontrados}
        for slug in minhas_turmas:
            if slug not in vistos_já:
                r = get(f"{BASE_SP}/sites/{slug}/_api/web/title", token, timeout=8)
                if r.status_code == 200:
                    adicionar(r.json().get("value", slug), slug)

    if not encontrados:
        print(
            "nenhuma turma encontrada"
        )
    return encontrados


# listar arquivos
def _parse_data(s):
    try:
        return datetime.fromisoformat(s[:19])
    except Exception:
        return datetime.now()



def listar_arquivos_pastas(token, slug, url_rel, profundidade=0):
    if profundidade > 5:
        return []
    base = f"{BASE_SP}/sites/{slug}/_api/web"
    enc = urllib.parse.quote(url_rel, safe="/:@")
    arquivos = []
    try:
        r = get(
            f"{base}/GetFolderByServerRelativeUrl('{enc}')/Files"
            f"?$select=Name,ServerRelativeUrl,TimeLastModified,Length,UniqueId",
            token,
        )
        if r.status_code == 200:
            itens = r.json().get("value", [])
            if isinstance(itens, dict):
                itens = itens.get("results", [])
            for item in itens:
                nome = item.get("Name", "")
                if Path(nome).suffix.lower() not in EXTENSOES:
                    continue
                url_rel_arq = item.get("ServerRelativeUrl", "")
                arquivos.append(
                    {
                        "nome": nome,
                        "url": BASE_SP + url_rel_arq,
                        "url_rel": url_rel_arq,
                        "slug": slug,
                        "modificado": _parse_data(item.get("TimeLastModified", "")),
                        "tamanho": int(item.get("Length", 0) or 0),
                        "id_unico": item.get(
                            "UniqueId", hashlib.md5(url_rel_arq.encode()).hexdigest()
                        ),
                    }
                )
    except Exception as e:
        if debug:
            print(f"    [aviso] Pastas: {e}")
    try:
        r = get(
            f"{base}/GetFolderByServerRelativeUrl('{enc}')/Folders"
            f"?$select=Name,ServerRelativeUrl",
            token,
        )
        if r.status_code == 200:
            subs = r.json().get("value", [])
            if isinstance(subs, dict):
                subs = subs.get("results", [])
            for sub in subs:
                nome_sub = sub.get("Name", "")
                if nome_sub.startswith(("_", "Forms")) or nome_sub in PASTAS_ALUNOS:
                    continue
                if sub.get("ServerRelativeUrl"):
                    arquivos.extend(
                        listar_arquivos_pastas(
                            token, slug, sub["ServerRelativeUrl"], profundidade + 1
                        )
                    )
    except Exception:
        pass
    return arquivos


def listar_arquivos_turma(token, slug):
    url = (
        f"{BASE_SP}/sites/{slug}/_api/web/lists"
        f"?$filter=BaseTemplate eq 101 and Hidden eq false"
        f"&$select=Title,RootFolder/ServerRelativeUrl&$expand=RootFolder"
    )
    try:
        r = get(url, token)
        listas = r.json().get("value", []) if r.status_code == 200 else []
        if isinstance(listas, dict):
            listas = listas.get("results", [])
    except Exception:
        listas = []
    listas = [
        l
        for l in listas
        if l.get("Title") not in LISTAS_SISTEMA | PASTAS_ALUNOS
        and not l.get("RootFolder", {})
        .get("ServerRelativeUrl", "")
        .endswith(("/SiteAssets", "/Style Library", "/FormServerTemplates"))
    ]
    if not listas:
        listas = [
            {"Title": n, "RootFolder": {"ServerRelativeUrl": f"/sites/{slug}/{n}"}}
            for n in (
                "Material de Aula",
                "Materiais",
                "Class Files",
                "Documents",
                "Documentos",
            )
        ]
    todos, vistos = [], set()
    for lista in listas:
        url_rel = lista.get("RootFolder", {}).get("ServerRelativeUrl", "")
        for arq in listar_arquivos_pastas(token, slug, url_rel):
            if arq["id_unico"] not in vistos:
                vistos.add(arq["id_unico"])
                todos.append(arq)
    return todos


def baixar_arquivo(token, arquivo, pasta_destino, numero=0):
    pasta_destino.mkdir(parents=True, exist_ok=True)
    nome_salvo = f"{numero:03d}_{arquivo['nome']}" if numero > 0 else arquivo["nome"]
    caminho = pasta_destino / nome_salvo
    if caminho.exists():
        return None

    print(f"    baixando: {nome_salvo} ", end="", flush=True)

    url_rel, slug = arquivo.get("url_rel", ""), arquivo.get("slug", "")
    urls = []
    if url_rel and slug:
        enc = urllib.parse.quote(url_rel, safe="/:@")
        urls.append(
            f"{BASE_SP}/sites/{slug}/_api/web/GetFileByServerRelativeUrl('{enc}')/$value"
        )
    if arquivo.get("url"):
        urls.append(arquivo["url"])

    for url in urls:
        try:
            r = requests.get(url, headers=hdrs(token), timeout=60, stream=True)
            if r.status_code == 401:
                global _flag_401
                _flag_401 = True
                print("token expirado")
                if caminho.exists():
                    caminho.unlink()
                return None
            if r.status_code == 200:
                with open(caminho, "wb") as f:
                    total = sum(
                        len(chunk) for chunk in r.iter_content(8192) if f.write(chunk)
                    )
                print(f"ok ({total/1024:.1f} KB)")
                return caminho
        except Exception as e:
            if debug:
                print(f"\n {e}", end="")

    print("erro")
    if caminho.exists():
        caminho.unlink()
    return None


# notificaçãotg
def _telegram_configurado() -> bool:
    if not telegram_ativo:
        return False
    faltando = [
        n
        for n, v in (
            ("telegram_token", telegram_token),
            ("telegram_chat_id", telegram_chat_id),
        )
        if not v
    ]
    if faltando:
        if debug:
            print(f": {', '.join(faltando)}")
        return False
    return True


def _telegram_base_url():
    return f"https://api.telegram.org/bot{telegram_token}"


def _tg_escape(texto: str) -> str:
    for c in r"\_*[]()~`>#+-=|{}.!":
        texto = texto.replace(c, f"\\{c}")
    return texto


def telegram_enviar_texto(mensagem: str) -> bool:
    if not _telegram_configurado():
        return False
    try:
        r = requests.post(
            f"{_telegram_base_url()}/sendMessage",
            data={
                "chat_id": telegram_chat_id,
                "text": mensagem,
                "parse_mode": "MarkdownV2",
            },
            timeout=20,
        )
        if r.status_code != 200:
            print(
                f"  [telegram] falha ao enviar texto (HTTP {r.status_code}): {r.text[:300]}"
            )
            return False
        return True
    except Exception as e:
        print(f"  [telegram] erro de conexão (texto): {e}")
        return False


def telegram_enviar_documento(caminho: Path, legenda: str = "") -> bool:
    if not _telegram_configurado():
        return False

    if not caminho.exists():
        return False

    if caminho.stat().st_size > telegram_tamanho_max:
        print(
            f" arquivo grande demais para envio direto "
            f"({caminho.stat().st_size/1024/1024:.1f} MB) enviando só o aviso em texto."
        )
        return telegram_enviar_texto(
            f"{legenda}\n\n(arquivo grande demais para anexar)"
        )

    for tentativa in range(1, 4):
        try:
            with open(caminho, "rb") as f:
                r = requests.post(
                    f"{_telegram_base_url()}/sendDocument",
                    data={
                        "chat_id": telegram_chat_id,
                        "caption": legenda,
                        "parse_mode": "MarkdownV2",
                    },
                    files={"document": (caminho.name, f)},
                    timeout=180,
                )
            if r.status_code == 200:
                print(f"  [telegram] notificado: {caminho.name}")
                return True
            print(
                f"  [telegram] falha (HTTP {r.status_code}), tentativa {tentativa}/3: {r.text[:300]}"
            )
            telegram_enviar_texto(
                f"{legenda}\n\n(não foi possivel anexar o arquivo automaticamente)"
            )
            return False
        except Exception as e:
            print(f"  [telegram] erro tentativa {tentativa}/3: {e}")
            if tentativa < 3:
                time.sleep(5 * tentativa)

    telegram_enviar_texto(f"{legenda}\n\n(falha ao anexar após 3 tentativas)")
    return False


def notificar_arquivo_novo(turma_nome: str, caminho: Path):
    if _telegram_configurado():
        legenda_tg = f"Novo material em *{_tg_escape(turma_nome)}*\n{_tg_escape(caminho.name)}"
        telegram_enviar_documento(caminho, legenda_tg)

    if _whatsapp_configurado():
        legenda_zap = f"Novo material em *{turma_nome}*\n{caminho.name}"
        whatsapp_enviar_arquivo(caminho, legenda_zap)




def _whatsapp_configurado() -> bool:
    if not whatsapp_ativo:
        return False
    if not whatsapp_numero:
        if debug:
            print("WHATSAPP_NUMERO não definido")
        return False
    try:
        r = requests.get(f"{whatsapp_servidor}/status", timeout=3)
        if r.status_code == 200 and r.json().get("pronto"):
            return True
        if debug:
            print("servidor do zap não está pronto")
        return False
    except Exception:
        if debug:
            print("servidor do zap offline  — rode: node whatsapp_server.js")
        return False


def whatsapp_enviar_texto(mensagem: str) -> bool:
    if not _whatsapp_configurado():
        return False
    try:
        r = requests.post(
            f"{whatsapp_servidor}/texto",
            json={"numero": whatsapp_numero, "texto": mensagem},
            timeout=20,
        )
        if r.status_code == 200:
            return True
        print(f"  [whatsapp] falha ao enviar texto (HTTP {r.status_code}): {r.text[:200]}")
        return False
    except Exception as e:
        print(f"  [whatsapp] erro de conexão (texto): {e}")
        return False


def whatsapp_enviar_arquivo(caminho: Path, legenda: str = "") -> bool:
    if not _whatsapp_configurado():
        return False
    if not caminho.exists():
        return False

    for tentativa in range(1, 4):
        try:
            r = requests.post(
                f"{whatsapp_servidor}/arquivo",
                json={
                    "numero": whatsapp_numero,
                    "caminho": str(caminho.resolve()),
                    "legenda": legenda,
                },
                timeout=120,
            )
            if r.status_code == 200:
                print(f"  [whatsapp] enviado: {caminho.name}")
                return True
            print(
                f"  [whatsapp] falha (HTTP {r.status_code}), tentativa {tentativa}/3: {r.text[:200]}"
            )
            if tentativa == 1:
                whatsapp_enviar_texto(f"{legenda}\n\n(não foi possível anexar o arquivo)")
            return False
        except Exception as e:
            print(f"  [whatsapp] erro tentativa {tentativa}/3: {e}")
            if tentativa < 3:
                time.sleep(5 * tentativa)

    whatsapp_enviar_texto(f"{legenda}\n\n(falha ao anexar após 3 tentativas)")
    return False


def verificar_token(token):
    try:
        if debug:
            c = _jwt_claims(token)
            print(f"  [debug] token aud={c.get('aud')} app={c.get('app_displayname')}")
        r = get(f"{BASE_SP}/_api/web/currentuser", token, timeout=10)
        if r.status_code == 200:
            d = r.json()
            print(f"logado como: {d.get('Title')} ({d.get('Email')})")
            return True
        print(f"token inválido (HTTP {r.status_code}).")
    except Exception as e:
        print(f"erro de conexão: {e}")
    return False


def executar_ciclo(token, ctrl):
    global _flag_401
    _flag_401 = False

    baixados_total = 0
    turmas = listar_turmas(token)
    if not turmas:
        return ctrl

    print(f"{len(turmas)} turma(s) encontrada(s):")
    for t in turmas:
        print(f"  - {t['nome']}")

    for turma in turmas:
        if _flag_401:
            break  # token expirou no meio
        slug, nome = turma["slug"], turma["nome"]
        print(f"\n[{nome}]")
        arquivos = listar_arquivos_turma(token, slug)
        if not arquivos:
            print("    (nenhum arquivo encontrado)")
            continue

        arquivos.sort(key=lambda a: a["modificado"])
        print(f"    {len(arquivos)} arquivo(s) encontrado(s)")

        ja_baixados = sum(1 for v in ctrl.values() if v.get("turma") == slug)
        proximo = ja_baixados + 1
        novos = 0

        fila_notif = []
        for arq in arquivos:
            if _flag_401:
                break
            uid = arq["id_unico"]
            if uid in ctrl:
                continue
            if baixar_arquivo(token, arq, pasta_base / slug, numero=proximo):
                ctrl[uid] = {
                    "nome": arq["nome"],
                    "turma": slug,
                    "numero": proximo,
                    "baixado_em": datetime.now().isoformat(),
                }
                salvar_controle(ctrl)
                caminho_baixado = (
                    pasta_base / slug / f"{ctrl[uid]['numero']:03d}_{arq['nome']}"
                )
                fila_notif.append((nome, caminho_baixado))
                proximo += 1
                novos += 1
                baixados_total += 1

        for turma_nome, caminho in fila_notif:
            notificar_arquivo_novo(turma_nome, caminho)

        if novos == 0 and not _flag_401:
            print("    tudo atualizado.")

    if not _flag_401:
        salvar_controle(ctrl)
        print(f"\n{baixados_total} novo(s) arquivo(s) baixado(s).")
    return ctrl


def main():
    global _flag_401
    perfil_dir = perfil_dir_padrao
    arquivo_token = arquivo_token_padrao
    headless = True

    primeiro_uso = not perfil_dir.exists() or not any(perfil_dir.iterdir())
    if primeiro_uso:
        print("nenhum perfil encontrado, abrindo browser para login...")
        if not fazer_login(perfil_dir):
            sys.exit(1)

    token = obter_token_valido(perfil_dir, arquivo_token, headless)
    if not token:
        print("[ERRO] não foi possível obter um token válido.")
        print("  delete a pasta do perfil e rode novamente para refazer o login.")
        sys.exit(1)

    if not verificar_token(token):
        print("tentando capturar novo token...")
        token = capturar_token(perfil_dir, headless)
        if not token or not verificar_token(token):
            print("não foi possível validar o token")
            sys.exit(1)
        arquivo_token.write_text(token, encoding="utf-8")

    pasta_base.mkdir(parents=True, exist_ok=True)
    ctrl = carregar_controle()

    def _salvar_ao_sair():
        salvar_controle(ctrl)

    atexit.register(_salvar_ao_sair)

    def _handler_sinal(sig, frame):
        salvar_controle(ctrl)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handler_sinal)
    try:
        signal.signal(signal.SIGBREAK, _handler_sinal)
    except AttributeError:
        pass

    print(f"verificando a cada {intervalo_watch}s\n")
    falhas_renovacao = 0

    try:
        while True:
            agora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

            restam = _segundos_para_expirar(token)
            if not _token_valido(token):
                print(f"[{agora}] token expira em {_fmt_tempo(restam)}, renovando...")
                novo = capturar_token(perfil_dir, headless)
                if novo:
                    token = novo
                    arquivo_token.write_text(token, encoding="utf-8")
                    falhas_renovacao = 0
                    print(
                        f"token renovado, expira em {_fmt_tempo(_segundos_para_expirar(token))}"
                    )
                else:
                    falhas_renovacao += 1
                    espera = min(120 * falhas_renovacao, 600)
                    print(
                        f"falha na renovação ({falhas_renovacao}x), tentando novamente em {espera}s..."
                    )
                    time.sleep(espera)
                    continue

            ctrl = executar_ciclo(token, ctrl)

            tentativas_401 = 0
            while _flag_401 and tentativas_401 < 3:
                tentativas_401 += 1
                print(
                    f"401 detectado durante o ciclo, renovando token "
                    f"(tentativa {tentativas_401}/3)..."
                )
                novo = capturar_token(perfil_dir, headless)
                if novo and novo != token:
                    token = novo
                    arquivo_token.write_text(token, encoding="utf-8")
                    falhas_renovacao = 0
                    print(
                        f"token renovado, expira em {_fmt_tempo(_segundos_para_expirar(token))}"
                    )
                    print("repetindo ciclo com novo token...")
                    ctrl = executar_ciclo(token, ctrl)
                elif novo == token:
                    print(
                        "ainda válido por tempo, mas rejeitado pela api"
                        "aguardando alguns segundos antes de tentar de novo"
                    )
                    time.sleep(15)
                    _flag_401 = False
                    novo2 = capturar_token(perfil_dir, headless)
                    if novo2 and novo2 != token:
                        token = novo2
                        arquivo_token.write_text(token, encoding="utf-8")
                        print(
                            f"token renovado, expira em {_fmt_tempo(_segundos_para_expirar(token))}"
                        )
                        ctrl = executar_ciclo(token, ctrl)
                    else:
                        break
                else:
                    falhas_renovacao += 1
                    print("falha na renovação, aguardando próximo ciclo...")
                    break

            if _flag_401:
                print(
                    "  [aviso] não foi possível obter um token aceito pela API "
                    "após múltiplas tentativas neste ciclo."
                )

            print(f"\npróxima verificação em {intervalo_watch}s...\n")
            time.sleep(intervalo_watch)

    except KeyboardInterrupt:
        print("encerrado.")
        salvar_controle(ctrl)


if __name__ == "__main__":
    main()
