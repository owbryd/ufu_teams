import os, json, time, hashlib, atexit, signal, sys
from pathlib import Path
from datetime import datetime
import requests


MOODLE_URL   = "https://moodle.ufu.br"
pasta_base   = Path(__file__).parent / "UFU_Moodle"
arquivo_ctrl = pasta_base / ".baixados.json"

EXTENSOES = {".pdf", ".pptx", ".ppt", ".docx", ".doc", ".xlsx", ".zip", ".txt"}

intervalo_watch = 300   # segundos entre verificações
debug           = False


moodle_usuario = os.environ.get("MOODLE_USUARIO", "")
moodle_senha   = os.environ.get("MOODLE_SENHA",   "")
_token_cache_file = Path(__file__).parent / "moodle_token.txt"


#tg
telegram_ativo       = True
telegram_token       = os.environ.get("TELEGRAM_TOKEN",   "")
telegram_chat_id     = os.environ.get("TELEGRAM_CHAT_ID", "")
telegram_tamanho_max = 49 * 1024 * 1024  # 49 MB

#zap
whatsapp_ativo    = True
whatsapp_numero   = os.environ.get("WHATSAPP_NUMERO",   "")
whatsapp_servidor = os.environ.get("WHATSAPP_SERVIDOR", "http://localhost:3737")


def obter_token(usuario: str, senha: str) -> str | None:
    """Faz login no Moodle e retorna o wstoken."""
    url = f"{MOODLE_URL}/login/token.php"
    try:
        r = requests.post(
            url,
            data={
                "username": usuario,
                "password": senha,
                "service":  "moodle_mobile_app",
            },
            headers={"Accept": "application/json"},
            timeout=20,
        )
        d = r.json()
        if "token" in d:
            return d["token"]
        print(f"erro no login: {d.get('error', d)}")
    except Exception as e:
        print(f"erro de conexão no login: {e}")
    return None


def obter_token_valido() -> str | None:
    if _token_cache_file.exists():
        t = _token_cache_file.read_text(encoding="utf-8").strip()
        if t:
            if _token_ok(t):
                return t
            print("token em cache inválido, refazendo login...")

    if not moodle_usuario or not moodle_senha:
        print("defina o email e senha")
        return None

    t = obter_token(moodle_usuario, moodle_senha)
    if t:
        _token_cache_file.write_text(t, encoding="utf-8")
        print("token salvo.")
    return t


def _token_ok(token: str) -> bool:
    try:
        r = ws(token, "core_webservice_get_site_info", timeout=10)
        return "sitename" in r
    except Exception:
        return False



def ws(token: str, wsfunction: str, params: dict | None = None, timeout: int = 20) -> dict | list:
    url = f"{MOODLE_URL}/webservice/rest/server.php"
    payload = {
        "moodlewsrestformat":    "json",
        "wstoken":               token,
        "wsfunction":            wsfunction,
        "moodlewssettingfilter": "true",
        "moodlewssettingfileurl":"true",
        "moodlewssettinglang":   "pt_br",
    }
    if params:
        payload.update(params)
    r = requests.post(url, data=payload, timeout=timeout)
    r.raise_for_status()
    d = r.json()
    if isinstance(d, dict) and "exception" in d:
        raise RuntimeError(f"Moodle API: {d.get('message', d)}")
    return d




def listar_disciplinas(token: str) -> list[dict]:
    #devolve as disciplinas
    info = ws(token, "core_webservice_get_site_info")
    userid = info.get("userid")
    if not userid:
        print("não foi possível obter o userid.")
        return []

    disciplinas = ws(token, "core_enrol_get_users_courses", {
        "userid":          userid,
        "returnusercount": 0,
    })
    if not isinstance(disciplinas, list):
        return []

    resultado = []
    for d in disciplinas:
        if d.get("hidden") or not d.get("visible", 1):
            continue
        resultado.append({
            "id":   d["id"],
            "nome": d.get("displayname") or d.get("fullname", str(d["id"])),
            "slug": str(d["id"]),
        })
    return resultado


def listar_arquivos_disciplina(token: str, course_id: int) -> list[dict]:
    try:
        secoes = ws(token, "core_course_get_contents", {
            "courseid":                          course_id,
            "options[0][name]":                  "excludemodules",
            "options[0][value]":                 0,
            "options[1][name]":                  "excludecontents",
            "options[1][value]":                 0,
            "options[2][name]":                  "includestealthmodules",
            "options[2][value]":                 1,
        })
    except Exception as e:
        print(f"  erro ao listar conteúdo da disciplina {course_id}: {e}")
        return []

    arquivos: list[dict] = []

    for secao in secoes:
        if not secao.get("uservisible", True):
            continue
        for modulo in secao.get("modules", []):
            if not modulo.get("uservisible", True):
                continue
            modname = modulo.get("modname", "")

            if modname == "resource":
                _coletar_de_contents(modulo.get("contents", []), arquivos)

            elif modname == "folder":
                _coletar_pasta_folder(token, course_id, modulo, arquivos)

    return arquivos


def _coletar_de_contents(contents: list, destino: list):
    for item in contents:
        if item.get("type") != "file":
            continue
        nome = item.get("filename", "")
        if Path(nome).suffix.lower() not in EXTENSOES:
            continue
        fileurl = item.get("fileurl", "")
        if not fileurl:
            continue
        destino.append({
            "nome":       nome,
            "url":        fileurl,
            "tamanho":    item.get("filesize", 0),
            "modificado": datetime.fromtimestamp(item.get("timemodified", 0)),
            "id_unico":   hashlib.md5(fileurl.encode()).hexdigest(),
        })


def _coletar_pasta_folder(token: str, course_id: int, modulo: dict, destino: list):
    cmid = modulo.get("id")
    try:
        secoes = ws(token, "core_course_get_contents", {
            "courseid":          course_id,
            "options[0][name]":  "includestealthmodules",
            "options[0][value]": 1,
            "options[1][name]":  "cmid",
            "options[1][value]": cmid,
        })
        for secao in secoes:
            for mod in secao.get("modules", []):
                if mod.get("id") == cmid:
                    _coletar_de_contents(mod.get("contents", []), destino)
                    return
    except Exception as e:
        if debug:
            print(f"    [aviso] folder cmid={cmid}: {e}")


def _calcular_hash(caminho: Path) -> str:
    h = hashlib.md5()
    with open(caminho, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def baixar_arquivo(token: str, arquivo: dict, pasta_destino: Path, numero: int = 0) -> tuple[Path | None, str | None]:
    pasta_destino.mkdir(parents=True, exist_ok=True)
    nome_salvo = f"{numero:03d}_{arquivo['nome']}" if numero > 0 else arquivo["nome"]
    caminho = pasta_destino / nome_salvo

    if caminho.exists():
        return None, None

    print(f"    baixando: {nome_salvo} ", end="", flush=True)
    url = arquivo["url"]
    if "?" in url:
        url_auth = f"{url}&token={token}"
    else:
        url_auth = f"{url}?token={token}"

    try:
        r = requests.get(url_auth, timeout=120, stream=True)
        if r.status_code == 401:
            print("não autorizado (token inválido?)")
            return None, None
        if r.status_code != 200:
            print(f"HTTP {r.status_code}")
            if caminho.exists():
                caminho.unlink()
            return None, None

        total = 0
        with open(caminho, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
                total += len(chunk)

        arquivo_hash = _calcular_hash(caminho)
        print(f"ok ({total / 1024:.1f} KB)")
        return caminho, arquivo_hash

    except Exception as e:
        print(f"erro: {e}")
        if caminho.exists():
            caminho.unlink()
        return None, None



def carregar_controle() -> dict:
    if arquivo_ctrl.exists():
        try:
            return json.loads(arquivo_ctrl.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def salvar_controle(ctrl: dict):
    arquivo_ctrl.parent.mkdir(parents=True, exist_ok=True)
    arquivo_ctrl.write_text(
        json.dumps(ctrl, indent=2, ensure_ascii=False), encoding="utf-8"
    )



def _telegram_configurado() -> bool:
    if not telegram_ativo:
        return False
    return bool(telegram_token and telegram_chat_id)


def _tg_escape(texto: str) -> str:
    for c in r"\_*[]()~`>#+-=|{}.!":
        texto = texto.replace(c, f"\\{c}")
    return texto


def telegram_enviar_texto(mensagem: str) -> bool:
    if not _telegram_configurado():
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{telegram_token}/sendMessage",
            data={
                "chat_id":    telegram_chat_id,
                "text":       mensagem,
                "parse_mode": "MarkdownV2",
            },
            timeout=20,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"  [telegram] erro: {e}")
        return False


def telegram_enviar_documento(caminho: Path, legenda: str = "") -> bool:
    if not _telegram_configurado():
        return False
    if not caminho.exists():
        return False
    if caminho.stat().st_size > telegram_tamanho_max:
        return telegram_enviar_texto(f"{legenda}\n\n(arquivo grande demais para anexar)")

    for tentativa in range(1, 4):
        try:
            with open(caminho, "rb") as f:
                r = requests.post(
                    f"https://api.telegram.org/bot{telegram_token}/sendDocument",
                    data={"chat_id": telegram_chat_id, "caption": legenda, "parse_mode": "MarkdownV2"},
                    files={"document": (caminho.name, f)},
                    timeout=180,
                )
            if r.status_code == 200:
                print(f"  [telegram] enviado: {caminho.name}")
                return True
            print(f"  [telegram] falha HTTP {r.status_code}, tentativa {tentativa}/3")
            telegram_enviar_texto(f"{legenda}\n\n(não foi possível anexar o arquivo)")
            return False
        except Exception as e:
            print(f"  [telegram] erro tentativa {tentativa}/3: {e}")
            if tentativa < 3:
                time.sleep(5 * tentativa)

    telegram_enviar_texto(f"{legenda}\n\n(falha ao anexar após 3 tentativas)")
    return False


def _whatsapp_configurado() -> bool:
    if not whatsapp_ativo or not whatsapp_numero:
        return False
    try:
        r = requests.get(f"{whatsapp_servidor}/status", timeout=3)
        return r.status_code == 200 and r.json().get("pronto", False)
    except Exception:
        if debug:
            print("servidor do zap offline — rode: node whatsapp_server.js")
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
        return r.status_code == 200
    except Exception as e:
        print(f"  [whatsapp] erro: {e}")
        return False


def whatsapp_enviar_arquivo(caminho: Path, legenda: str = "") -> bool:
    if not _whatsapp_configurado() or not caminho.exists():
        return False
    for tentativa in range(1, 4):
        try:
            r = requests.post(
                f"{whatsapp_servidor}/arquivo",
                json={
                    "numero":  whatsapp_numero,
                    "caminho": str(caminho.resolve()),
                    "legenda": legenda,
                },
                timeout=120,
            )
            if r.status_code == 200:
                print(f"  [whatsapp] enviado: {caminho.name}")
                return True
            print(f"  [whatsapp] falha HTTP {r.status_code}, tentativa {tentativa}/3")
            if tentativa == 1:
                whatsapp_enviar_texto(f"{legenda}\n\n(não foi possível anexar o arquivo)")
            return False
        except Exception as e:
            print(f"  [whatsapp] erro tentativa {tentativa}/3: {e}")
            if tentativa < 3:
                time.sleep(5 * tentativa)

    whatsapp_enviar_texto(f"{legenda}\n\n(falha ao anexar após 3 tentativas)")
    return False


def notificar_arquivo_novo(disc_nome: str, caminho: Path):
    if _telegram_configurado():
        legenda = f"Novo material em *{_tg_escape(disc_nome)}*\n{_tg_escape(caminho.name)}"
        telegram_enviar_documento(caminho, legenda)

    if _whatsapp_configurado():
        legenda = f"Novo material em *{disc_nome}*\n{caminho.name}"
        whatsapp_enviar_arquivo(caminho, legenda)



def executar_ciclo(token: str, ctrl: dict) -> dict:
    disciplinas = listar_disciplinas(token)
    if not disciplinas:
        print("nenhuma disciplina encontrada.")
        return ctrl

    print(f"{len(disciplinas)} disciplina(s):")
    for d in disciplinas:
        print(f"  - {d['nome']}")

    baixados_total = 0

    for disc in disciplinas:
        slug, nome, course_id = disc["slug"], disc["nome"], disc["id"]
        print(f"\n[{nome}]")

        arquivos = listar_arquivos_disciplina(token, course_id)
        if not arquivos:
            print("    (nenhum arquivo encontrado)")
            continue

        arquivos.sort(key=lambda a: a["modificado"])
        print(f"    {len(arquivos)} arquivo(s) encontrado(s)")

        numeros_usados = [v.get("numero", 0) for v in ctrl.values() if v.get("slug") == slug]
        proximo = (max(numeros_usados) + 1) if numeros_usados else 1
        hashes_baixados = {
            v["hash"] for v in ctrl.values()
            if v.get("slug") == slug and v.get("hash")
        }
        novos = 0
        fila_notif: list[tuple[str, Path]] = []

        for arq in arquivos:
            uid = arq["id_unico"]
            if uid in ctrl:
                continue

            pasta_dest = pasta_base / slug
            caminho_baixado, arquivo_hash = baixar_arquivo(token, arq, pasta_dest, numero=proximo)

            if caminho_baixado:
                if arquivo_hash and arquivo_hash in hashes_baixados:
                    print(f"    duplicata detectada (hash igual), removendo: {caminho_baixado.name}")
                    caminho_baixado.unlink()
                    ctrl[uid] = {
                        "nome":       arq["nome"],
                        "slug":       slug,
                        "numero":     -1,
                        "hash":       arquivo_hash,
                        "duplicata":  True,
                        "baixado_em": datetime.now().isoformat(),
                    }
                    salvar_controle(ctrl)
                    continue

                ctrl[uid] = {
                    "nome":       arq["nome"],
                    "slug":       slug,
                    "numero":     proximo,
                    "hash":       arquivo_hash,
                    "baixado_em": datetime.now().isoformat(),
                }
                if arquivo_hash:
                    hashes_baixados.add(arquivo_hash)
                salvar_controle(ctrl)
                fila_notif.append((nome, caminho_baixado))
                proximo += 1
                novos += 1
                baixados_total += 1

        for disc_nome, caminho in fila_notif:
            notificar_arquivo_novo(disc_nome, caminho)

        if novos == 0:
            print("    tudo atualizado.")

    salvar_controle(ctrl)
    print(f"\n{baixados_total} novo(s) arquivo(s) baixado(s).")
    return ctrl



def main():
    token = obter_token_valido()
    if not token:
        print("não foi possível obter um token válido. encerrando.")
        sys.exit(1)

    if not _token_ok(token):
        print("token inválido ou expirado, refazendo login...")
        _token_cache_file.unlink(missing_ok=True)
        token = obter_token_valido()
        if not token or not _token_ok(token):
            print("não foi possível validar o token.")
            sys.exit(1)

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

    print(f"monitorando moodle a cada {intervalo_watch}s\n")

    try:
        while True:
            print(f"verificando...")

            #o token n expira, se deu erro refaz o login
            ctrl = executar_ciclo(token, ctrl)

            print(f"\npróxima verificação em {intervalo_watch}s...\n")
            time.sleep(intervalo_watch)

    except KeyboardInterrupt:
        print("encerrado.")
        salvar_controle(ctrl)


if __name__ == "__main__":
    main()
