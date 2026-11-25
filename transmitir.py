#!/usr/bin/env python3
"""Preenche a NFC-e do ZWeb na ordem da tela e não clica em Transmitir.

Ordem, igual às fotos da cliente:
1. Fiscal > NFC-e > Cadastrar NFC-e
2. Produtos: busca pelo código (ex. 54 # PÃO DE BATATA), quantidade, inclui o item
3. Formas de pagamento: 4 Cartão, valor, parcelamento, bandeira, número da transação
4. Para. O botão Transmitir não é clicado.
"""

from __future__ import annotations

# EMITE_NOTAS_ROBO
ROBO_VERSAO = "2026-11-25.2"

import argparse
import json
import os
import random
import re
import socket
import subprocess
import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PwTimeout
from playwright.sync_api import sync_playwright

PROIBIDO = re.compile(r"transmitir|cancelar|fechar caixa|sangria", re.I)
PASTA_NOTAS = Path(__file__).resolve().parents[2] / "Notas_emitidas"


def emit(event: str, **kw) -> None:
    print(json.dumps({"event": event, **kw}, ensure_ascii=False), flush=True)


def centavos_campo(centavos: int) -> str:
    sinal = "-" if centavos < 0 else ""
    centavos = abs(int(centavos))
    reais, cents = divmod(centavos, 100)
    milhar = f"{reais:,}".replace(",", ".")
    return f"{sinal}{milhar},{cents:02d}"


def porta_aberta(porta: int) -> bool:
    s = socket.socket()
    s.settimeout(0.4)
    try:
        s.connect(("127.0.0.1", porta))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _chrome_executavel() -> str:
    if os.name == "nt":
        locais = [
            os.environ.get("PROGRAMFILES", r"C:\Program Files")
            + r"\Google\Chrome\Application\chrome.exe",
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
            + r"\Google\Chrome\Application\chrome.exe",
            str(Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe"),
        ]
        for caminho in locais:
            if Path(caminho).exists():
                return caminho
        return "chrome"
    return "google-chrome"


def _area_trabalho() -> tuple[int, int, int, int]:
    if os.name == "nt":
        import ctypes

        class RECT(ctypes.Structure):
            _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]

        rect = RECT()
        if ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0):
            return rect.l, rect.t, max(1, rect.r - rect.l), max(1, rect.b - rect.t)
    else:
        try:
            saida = subprocess.check_output(["xprop", "-root", "_NET_WORKAREA"], text=True, timeout=2)
            nums = [int(p.strip()) for p in saida.split("=", 1)[1].split(",")]
            if len(nums) >= 4 and nums[2] > 0 and nums[3] > 0:
                return nums[0], nums[1], nums[2], nums[3]
        except Exception:
            pass
    return 0, 0, 1920, 1080


def garantir_chrome(porta: int) -> None:
    if porta_aberta(porta):
        return
    perfil = _pasta_dados() / "chrome"
    perfil.mkdir(parents=True, exist_ok=True)
    emit("log", msg="O Firefox não serve. Abrindo um Chrome separado…")
    x, y, w, h = _area_trabalho()
    comando = [
        _chrome_executavel(),
        f"--remote-debugging-port={porta}",
        f"--user-data-dir={perfil}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--hide-crash-restore-bubble",
        "--new-window",
        "--start-maximized",
        f"--window-position={x},{y}",
        f"--window-size={w},{h}",
        "https://zweb.com.br/#/sign-in",
    ]
    opcoes = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        opcoes["creationflags"] = 0x00000008 | 0x00000200
    else:
        opcoes["start_new_session"] = True
    subprocess.Popen(comando, **opcoes)
    for _ in range(50):
        if porta_aberta(porta):
            return
        time.sleep(0.2)
    emit("erro", msg="O Chrome não abriu. Confira se o google-chrome está instalado.")
    raise SystemExit(2)


def conectar(p, porta: int):
    garantir_chrome(porta)
    url = f"http://127.0.0.1:{porta}"
    try:
        return p.chromium.connect_over_cdp(url)
    except Exception as e:
        emit("erro", msg=f"O Chrome abriu, mas a conexão falhou. Detalhe: {e}")
        raise SystemExit(2)


def sessao_zweb(page) -> bool:
    page.wait_for_timeout(800)
    url = (page.url or "").lower()
    if "zweb.com.br" not in url:
        return False
    if any(p in url for p in ("sign-in", "signin", "/login", "forgot-password")):
        return False
    try:
        texto = page.locator("body").inner_text(timeout=8000).lower()
    except Exception:
        texto = ""
    if "senha" in texto and any(p in texto for p in ("entrar", "e-mail", "email")):
        return False
    if any(p in url for p in ("#/dashboard", "#/fiscal", "#/account")):
        return True
    return any(p in texto for p in ("dashboard", "cadastrar nfc-e", "nfc-e"))


def pagina_zweb(browser):
    for ctx in browser.contexts:
        for page in ctx.pages:
            if "zweb" in (page.url or "").lower():
                return page
    if browser.contexts:
        page = browser.contexts[0].new_page()
        page.goto("https://zweb.com.br/#/fiscal/nfce", wait_until="domcontentloaded")
        return page
    emit("erro", msg="Chrome aberto, mas sem janela. Abra o ZWeb e tente de novo.")
    raise SystemExit(3)


def _visivel(locator) -> bool:
    try:
        return locator.count() > 0 and locator.first.is_visible()
    except Exception:
        return False


def abrir_secao(page, heading: str) -> None:
    btn = page.locator(f"#{heading} button.accordion-button")
    btn.wait_for(state="visible", timeout=20000)
    if "collapsed" not in (btn.get_attribute("class") or ""):
        return
    btn.evaluate("el => el.scrollIntoView({block: 'center', inline: 'nearest'})")
    page.wait_for_timeout(200)
    try:
        btn.click(timeout=4000)
    except Exception:
        btn.evaluate("el => el.click()")
    page.wait_for_timeout(400)
    if "collapsed" in (btn.get_attribute("class") or ""):
        btn.evaluate("el => el.click()")
        page.wait_for_timeout(400)


def fechar_avisos(page) -> None:
    avisos = page.locator(".modal.show, .swal2-popup, [role='dialog']").locator("button").filter(
        has_text=re.compile(r"^(OK|Ok|Fechar|Entendi|Continuar)$")
    )
    for _ in range(3):
        if not _visivel(avisos):
            return
        try:
            avisos.first.click(timeout=2000)
        except Exception:
            return
        page.wait_for_timeout(250)


def clicar_cadastrar(page) -> None:
    botoes = page.locator("button.btn-primary").filter(
        has_text=re.compile(r"Cadastrar\s+NFC-?e", re.I)
    )
    alvo = None
    for i in range(botoes.count()):
        botao = botoes.nth(i)
        try:
            if not botao.is_visible():
                continue
        except Exception:
            continue
        cls = botao.get_attribute("class") or ""
        if "btn-block" not in cls:
            alvo = botao
            break
        alvo = alvo or botao
    if alvo is None:
        page.goto("https://zweb.com.br/#/fiscal/nfce/new", wait_until="domcontentloaded")
        return
    alvo.click()


_LISTA_NFCE = re.compile(r"#/fiscal/nfce(?!/new)(?:$|[/?#])")
_VALOR_LISTA = re.compile(r"-?\d[\d.]*,\d{2}")
_DANFE = re.compile(r"/danfe/(\d{44})")


def na_lista_nfce(url: str) -> bool:
    return _LISTA_NFCE.search(url or "") is not None


def interpretar_linhas_nfce(brutas: list) -> list[dict]:
    """Cada linha da grade: número, status e total. A primeira coluna numérica é o número."""
    saida = []
    for celulas in brutas:
        textos = [" ".join(str(c).split()) for c in celulas if str(c).strip()]
        numeros = [int(c) for c in textos if re.fullmatch(r"\d+", c) and len(c) < 12]
        if not numeros:
            continue
        status = ""
        for c in textos:
            if re.search(r"autoriz|rejeit|deneg|cancel|process|erro|penden", c, re.I):
                status = c
                break
        totais = [m.group(0) for c in textos if (m := _VALOR_LISTA.search(c))]
        saida.append(
            {
                "numero": numeros[0],
                "status": status,
                "total": totais[-1] if totais else "",
            }
        )
    return saida


def ler_numeros_lista(page) -> set[int]:
    return {linha["numero"] for linha in ler_linhas_lista(page)}


def ler_linhas_lista(page) -> list[dict]:
    """A grade do ZWeb é div.table-row, não a tag table."""
    try:
        brutas = page.evaluate(
            """() => {
              const texto = el => {
                const vis = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                const dica = el.getAttribute('title')
                  || (el.querySelector('[title]') && el.querySelector('[title]').getAttribute('title'))
                  || '';
                const extra = (dica || '').replace(/\\s+/g, ' ').trim();
                return extra && !vis.includes(extra) ? (vis + ' ' + extra).trim() : vis;
              };
              const grade = document.querySelector('.table-wrapper div.table');
              if (grade) {
                const linhas = [...grade.querySelectorAll(':scope > .table-row')]
                  .filter(r => !r.classList.contains('header'));
                if (linhas.length) {
                  return linhas.map(r => [...r.querySelectorAll(':scope > .cell')].map(texto));
                }
              }
              const tabelas = [...document.querySelectorAll('table')];
              const tabela = tabelas.find(t => /N[uú]mero/i.test(t.innerText) && /Status/i.test(t.innerText));
              if (!tabela) return [];
              return [...tabela.querySelectorAll('tbody tr')].map(tr =>
                [...tr.querySelectorAll('td')].map(texto)
              );
            }"""
        )
    except Exception:
        return []
    if not isinstance(brutas, list):
        return []
    return interpretar_linhas_nfce(brutas)


def chave_danfe(url: str) -> str | None:
    texto = url or ""
    m = _DANFE.search(texto)
    if m:
        return m.group(1)
    baixo = texto.lower()
    if "danfe" in baixo or ".pdf" in baixo:
        solta = re.search(r"(\d{44})", texto)
        if solta:
            return solta.group(1)
    return None


def _paginas(page) -> list:
    try:
        return list(page.context.pages)
    except Exception:
        return [page]


def danfes_abertos(page) -> set[int]:
    """Números de NFC-e cujo DANFE está aberto em alguma aba."""
    numeros = set()
    for candidato in _paginas(page):
        try:
            url = candidato.url or ""
        except Exception:
            continue
        chave = chave_danfe(url)
        numero = numero_da_chave(chave) if chave else None
        if numero:
            numeros.add(numero)
    return numeros


def numero_da_chave(chave: str) -> int | None:
    if len(chave) < 34 or not chave.isdigit():
        return None
    try:
        return int(chave[25:34])
    except ValueError:
        return None


def _voltar_da_danfe(page) -> None:
    chave = chave_danfe(page.url or "")
    if not chave:
        return
    numero = numero_da_chave(chave)
    emit("log", msg=f"O ZWeb abriu o DANFE da NFC-e {numero}. Voltando à lista.")
    page.goto("https://zweb.com.br/#/fiscal/nfce", wait_until="domcontentloaded")
    page.wait_for_timeout(800)


def salvar_recibo(aba, chave: str) -> bool:
    """Copia o PDF do recibo para Notas_emitidas. Se já estiver lá, não baixa de novo."""
    PASTA_NOTAS.mkdir(parents=True, exist_ok=True)
    destino = PASTA_NOTAS / f"{chave}.pdf"
    if destino.is_file() and destino.stat().st_size > 500:
        return True
    resposta = aba.context.request.get(aba.url, timeout=20000)
    if not resposta.ok:
        return False
    corpo = resposta.body()
    if not corpo.startswith(b"%PDF"):
        return False
    destino.write_bytes(corpo)
    return destino.is_file() and destino.stat().st_size > 500


def recolher_recibos(page, somente: set[int] | None = None) -> None:
    """Salva o DANFE e fecha a aba. A aba do ZWeb volta para a lista."""
    for aba in list(_paginas(page)):
        try:
            chave = chave_danfe(aba.url or "")
        except Exception:
            continue
        if not chave:
            continue
        numero = numero_da_chave(chave)
        if numero is None or (somente is not None and numero not in somente):
            continue
        try:
            salvo = salvar_recibo(aba, chave)
        except Exception as e:
            emit("log", msg=f"Não consegui salvar o recibo da NFC-e {numero}. {e}")
            continue
        if not salvo:
            emit("log", msg=f"Não consegui salvar o recibo da NFC-e {numero}. A aba continua aberta.")
            continue
        if aba is page:
            emit("log", msg=f"Recibo da NFC-e {numero} salvo em Notas_emitidas. Voltando à lista.")
            page.goto("https://zweb.com.br/#/fiscal/nfce", wait_until="domcontentloaded")
            page.wait_for_timeout(400)
            continue
        try:
            aba.close()
            emit("log", msg=f"Recibo da NFC-e {numero} salvo em Notas_emitidas e a aba foi fechada.")
        except Exception as e:
            emit("log", msg=f"Recibo da NFC-e {numero} salvo, mas a aba não fechou. {e}")


def _esperar_lista_ou_danfe(page) -> bool:
    """Depois do Transmitir o ZWeb volta à lista ou abre o DANFE na mesma aba."""
    limite = time.time() + 25
    while time.time() < limite:
        url = page.url or ""
        if na_lista_nfce(url) or chave_danfe(url):
            return True
        page.wait_for_timeout(400)
    return False


def _mesmo_total(visto: str, esperado: str) -> bool:
    def cents(texto: str) -> int | None:
        m = _VALOR_LISTA.search(texto or "")
        if not m:
            return None
        limpo = m.group(0).replace(".", "").replace(",", "")
        if limpo.startswith("-"):
            return -int(limpo[1:])
        return int(limpo)
    a, b = cents(visto), cents(esperado)
    return a is not None and a == b


def esperar_nota_na_lista(
    page, nota: dict, anteriores: set[int], danfes_antes: set[int] | None = None, itens: list | None = None
) -> None:
    """A próxima nota só começa quando a lista ou um DANFE novo mostra esta, autorizada."""
    alvo = centavos_campo(int(nota["brutoCentavos"]))
    ja_abertos = danfes_antes or set()
    emit("log", msg=f"Aguardando a lista com a nota de {alvo}…")
    limite = time.time() + 90
    atualizou = False
    while time.time() < limite:
        fechar_avisos(page)
        novos = danfes_abertos(page) - ja_abertos - anteriores
        if novos:
            numero = min(novos)
            emit("log", msg=f"Nota {numero} autorizada no DANFE.")
            recolher_recibos(page, novos)
            return
        if not na_lista_nfce(page.url or ""):
            page.wait_for_timeout(600)
            continue
        linhas = ler_linhas_lista(page)
        for linha in linhas:
            if linha["numero"] in anteriores:
                continue
            if not _mesmo_total(linha["total"], alvo):
                continue
            status = linha["status"].lower()
            if "autoriz" in status:
                emit(
                    "log",
                    msg=f"Nota {linha['numero']} autorizada na lista ({linha['total']}).",
                )
                recolher_recibos(page)
                return
            if any(p in status for p in ("rejeit", "deneg", "erro")):
                cod = _bloquear_item_rejeitado(linha["status"], itens)
                extra = f" O produto {cod} fica de fora da próxima." if cod else ""
                raise RuntimeError(
                    f"A nota {linha['numero']} foi rejeitada ({linha['status']}).{extra} "
                    "Ela não foi autorizada, então o comprovante continua pendente."
                )
        if not atualizou and time.time() > limite - 70:
            atualizou = True
            emit("log", msg="Atualizando a lista.")
            page.goto("https://zweb.com.br/#/fiscal/nfce", wait_until="domcontentloaded")
            page.wait_for_timeout(800)
            continue
        page.wait_for_timeout(800)
    raise RuntimeError(f"A lista não mostrou a nota de {alvo} autorizada.")


def nova_nfce(page) -> set[int]:
    """Reabre o cadastro pela lista. Devolve os números que já estavam nela."""
    emit("log", msg="Abrindo a tela da NFC-e")
    fechar_avisos(page)
    if not na_lista_nfce(page.url or ""):
        page.goto("https://zweb.com.br/#/fiscal/nfce", wait_until="domcontentloaded")
        page.wait_for_timeout(700)
    fechar_avisos(page)
    try:
        page.locator(".table-row:not(.header), table tbody tr").first.wait_for(
            state="visible", timeout=8000
        )
    except PwTimeout:
        pass
    numeros = ler_numeros_lista(page)
    clicar_cadastrar(page)
    try:
        page.locator("#heading_fiscalNFCeProductsData").wait_for(state="visible", timeout=20000)
    except PwTimeout:
        emit("log", msg="O cadastro não abriu. Voltando à lista e tentando de novo.")
        page.goto("https://zweb.com.br/#/fiscal/nfce", wait_until="domcontentloaded")
        page.wait_for_timeout(800)
        fechar_avisos(page)
        numeros = ler_numeros_lista(page)
        clicar_cadastrar(page)
        page.locator("#heading_fiscalNFCeProductsData").wait_for(state="visible", timeout=20000)
    abrir_secao(page, "heading_fiscalNFCeProductsData")
    return numeros


def _opcao(page, texto: str):
    exato = re.compile(rf"^\s*{re.escape(texto)}\s*$", re.I)
    for papel in ("option", "listitem", "menuitem"):
        item = page.get_by_role(papel, name=exato)
        if _visivel(item):
            return item.first
    item = page.locator("li, [role='option'], a, div, span").filter(has_text=exato)
    if _visivel(item):
        return item.first
    return None


def escolher_lista(page, rotulo: str, opcoes: list[str]) -> None:
    rot = page.get_by_text(rotulo, exact=True).first
    rot.scroll_into_view_if_needed()
    rot.locator("xpath=following::*[self::div or self::input or self::button or self::span][1]").click()
    page.wait_for_timeout(350)
    for op in opcoes:
        alvo = _opcao(page, op)
        if alvo is not None:
            alvo.click()
            page.wait_for_timeout(250)
            return
    raise RuntimeError(f"Não achei {opcoes} no campo {rotulo}")


def tentar_lista(page, rotulo: str, opcoes: list[str]) -> None:
    try:
        escolher_lista(page, rotulo, opcoes)
    except Exception as e:
        emit("log", msg=f"{rotulo}: deixei o valor que já estava na tela ({e})")


def campo_depois(page, rotulo: str):
    lab = page.get_by_text(re.compile(rotulo, re.I)).first
    lab.scroll_into_view_if_needed()
    return lab.locator("xpath=following::input[1]")


def digitar_campo(campo, valor: str) -> None:
    campo.click(timeout=8000)
    campo.fill("")
    campo.type(valor, delay=35)


def fechar_menus(page) -> None:
    """Um menu aberto cobre o campo de baixo e o clique não chega nele."""
    page.evaluate(
        """() => {
          document.querySelectorAll('.multiselect--active').forEach(el => {
            let inst = el.__vueParentComponent;
            for (let i = 0; i < 6 && inst; i++) {
              const vm = inst.proxy || inst.ctx;
              if (vm && typeof vm.deactivate === 'function') { vm.deactivate(); break; }
              inst = inst.parent;
            }
            el.classList.remove('multiselect--active');
            const lista = el.querySelector('.multiselect__content-wrapper');
            if (lista) lista.style.display = 'none';
          });
        }"""
    )


def _botao_incluir(secao):
    """O + no fim da linha. O ZWeb usa #icon-add; a classe antiga era font-icon-add."""
    return secao.locator("button:has(#icon-add), button:has(.fa-plus), button:has(.font-icon-add)")


def clicar_icone_incluir(page, secao) -> None:
    """Botão da folha com sinal de +. Nunca é o Transmitir."""
    fechar_menus(page)
    botao = _botao_incluir(secao)
    botao.first.wait_for(state="visible", timeout=8000)
    for _ in range(40):
        if botao.first.is_enabled():
            botao.first.click()
            page.wait_for_timeout(400)
            parar_se_servidor_falhou(page)
            return
        page.wait_for_timeout(150)
    raise RuntimeError("O botão de incluir (folha com +) continuou desabilitado.")


def ler_saldo(page) -> tuple[str, bool] | None:
    """Saldo vermelho: ainda falta pagamento. Azul: o valor já entrou."""
    try:
        info = page.evaluate(
            """() => {
              const rotulos = [...document.querySelectorAll('span')]
                .filter(s => (s.textContent || '').trim() === 'Saldo');
              const rotulo = rotulos.at(-1);
              if (!rotulo) return null;
              const linha = rotulo.closest('.row') || rotulo.parentElement;
              const valor = linha && linha.querySelector('.redTotalizer, .blueTotalizer');
              if (!valor) return null;
              return {
                texto: (valor.textContent || '').replace(/\\s+/g, ' ').trim(),
                vermelho: valor.classList.contains('redTotalizer')
              };
            }"""
        )
    except Exception:
        return None
    if not isinstance(info, dict):
        return None
    return str(info.get("texto") or ""), bool(info.get("vermelho"))


def _centavos_de_texto(texto: str) -> int | None:
    m = _VALOR_LISTA.search(texto or "")
    if not m:
        return None
    limpo = m.group(0).replace(".", "").replace(",", "")
    if limpo.startswith("-"):
        return -int(limpo[1:])
    return int(limpo)


def confirmar_pagamento_incluido(page, secao, forma: str = "Cartão", refazer=None) -> None:
    for tentativa in range(2):
        for _ in range(8):
            saldo = ler_saldo(page)
            if saldo and not saldo[1]:
                emit("log", msg=f"Saldo {saldo[0]}.")
                return
            page.wait_for_timeout(250)
        if tentativa == 0:
            saldo = ler_saldo(page)
            faltam = _centavos_de_texto(saldo[0]) if saldo else None
            if refazer:
                try:
                    ja_listado = secao.locator("table").inner_text(timeout=1000)
                except Exception:
                    ja_listado = ""
                if _VALOR_LISTA.search(ja_listado or ""):
                    raise RuntimeError(
                        f"O pagamento aparece na lista, mas o saldo continuou vermelho ({saldo[0] if saldo else ''})."
                    )
                emit("log", msg="O saldo ainda está vermelho. Preenchendo o pagamento de novo.")
                refazer()
            else:
                botao_add = _botao_incluir(secao)
                try:
                    habilitado = botao_add.first.is_enabled()
                except Exception:
                    habilitado = False
                if not habilitado and faltam:
                    emit(
                        "log",
                        msg=f"O saldo ainda está vermelho ({saldo[0]}). Recolocando esse valor no cartão.",
                    )
                    cartao = ativar_forma(page, forma)
                    digitar_valor_forma(page, cartao, faltam)
                else:
                    emit("log", msg="O saldo ainda está vermelho. Incluindo o pagamento de novo.")
            clicar_icone_incluir(page, secao)
    saldo = ler_saldo(page)
    if saldo and saldo[1]:
        raise RuntimeError(
            f"O saldo continuou vermelho ({saldo[0]}). "
            "O pagamento não entrou na nota, então o Transmitir não foi clicado."
        )


def digitar_mascara(page, campo, centavos: int) -> None:
    """O ZWeb trata o que é digitado como centavos: 300 vira 3,00."""
    campo.click()
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")
    page.keyboard.type(str(abs(int(centavos))), delay=40)


def _sem_acento(texto: str) -> str:
    trocas = str.maketrans(
        "áàâãäéèêëíìîïóòôõöúùûüç",
        "aaaaaeeeeiiiiooooouuuuc",
    )
    return " ".join(texto.translate(trocas).lower().split())


def _casa_opcao(opcao: str, desejado: str) -> bool:
    a, b = _sem_acento(opcao), _sem_acento(desejado)
    if not a or not b or "nenhum" in a or "carreg" in a or "vazia" in a:
        return False
    return a == b or (len(b) >= 4 and b in a)


def _campo_multiselect(page, input_id: str):
    """O id fica no campo de busca. O menu em si é o combobox com aria-owns."""
    todos = page.locator(f"[aria-owns='listbox-{input_id}']")
    visiveis = todos.filter(visible=True)
    if visiveis.count():
        return visiveis.first
    return todos.first


def _textos_opcoes(wrap) -> list[str]:
    try:
        brutos = wrap.evaluate(
            """el => [...el.querySelectorAll('.multiselect__option')]
              .map(n => (n.textContent || '').replace(/\\s+/g, ' ').trim())
              .filter(Boolean)"""
        )
    except Exception:
        return []
    if not isinstance(brutos, list):
        return []
    return [" ".join(str(t).split()) for t in brutos if str(t).strip()]


def _opcoes_reais(opcoes: list[str]) -> list[str]:
    saida = []
    for texto in opcoes:
        normal = _sem_acento(texto)
        if not normal or "nenhum" in normal or "carreg" in normal or "vazia" in normal:
            continue
        saida.append(texto)
    return saida


def _marcar_opcao(wrap, desejado: str) -> str:
    """Marca pelo componente do menu. O clique na tela pode cair em Dinheiro."""
    try:
        marcado = wrap.evaluate(
            """(el, desejado) => {
              const sem = s => (s || '')
                .normalize('NFD')
                .replace(/[\\u0300-\\u036f]/g, '')
                .toLowerCase()
                .replace(/\\s+/g, ' ')
                .trim();
              const alvo = sem(desejado);
              const casa = texto => {
                const a = sem(texto);
                if (!a || a.includes('nenhum') || a.includes('carreg') || a.includes('vazia')) return false;
                return a === alvo || (alvo.length >= 4 && a.includes(alvo));
              };
              const vm = el.__vueParentComponent && (el.__vueParentComponent.proxy || el.__vueParentComponent.ctx);
              const rotulo = o => {
                if (!o) return '';
                if (vm && typeof vm.getOptionLabel === 'function') return String(vm.getOptionLabel(o) || '');
                return String(o.name || o.label || '');
              };
              if (vm && typeof vm.select === 'function') {
                const atual = vm.internalValue && vm.internalValue[0] ? rotulo(vm.internalValue[0]) : '';
                if (casa(atual)) return atual;
                const lista = Array.isArray(vm.filteredOptions) ? vm.filteredOptions : [];
                const opt = lista.find(o => sem(rotulo(o)) === alvo) || lista.find(o => casa(rotulo(o)));
                if (opt) {
                  vm.select(opt);
                  const novo = vm.internalValue && vm.internalValue[0] ? rotulo(vm.internalValue[0]) : rotulo(opt);
                  return casa(novo) ? novo : '';
                }
              }
              return '';
            }""",
            desejado,
        )
    except Exception:
        return ""
    return " ".join(str(marcado or "").split())


def _abrir_multiselect(wrap) -> None:
    classe = wrap.get_attribute("class", timeout=4000) or ""
    if "multiselect--active" in classe:
        return
    seta = wrap.locator(".multiselect__select")
    if seta.count():
        seta.first.evaluate(
            "el => el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true}))"
        )
        return
    wrap.click(timeout=4000)


def escolher_multiselect(
    page, input_id: str, textos: list[str], aceitar_primeira: bool = False
) -> None:
    """Marca no menu. A lista do ZWeb chega depois que a tela abre."""
    resultado: dict = {}
    for tentativa in range(12):
        try:
            lido = page.evaluate(
                """([inputId, desejados, abrir, aceitarPrimeira]) => {
                  const sem = s => (s || '')
                    .normalize('NFD')
                    .replace(/[\\u0300-\\u036f]/g, '')
                    .toLowerCase()
                    .replace(/\\s+/g, ' ')
                    .trim();
                  const casa = (texto, desejado) => {
                    const a = sem(texto);
                    const b = sem(desejado);
                    if (!a || !b || a.includes('nenhum') || a.includes('carreg') || a.includes('vazia')) return false;
                    return a === b || (b.length >= 4 && a.includes(b));
                  };
                  const visivel = el => {
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
                  };
                  const porId = id => [...document.querySelectorAll(`[aria-owns='listbox-${id}']`)].find(visivel) || null;
                  let el = porId(inputId);
                  if (!el && inputId === 'financeFlag') {
                    const conhecidos = ['category', 'finishingMode', 'paymentMode', 'financeCardAdministrator', 'financeFlag'];
                    const menus = [...document.querySelectorAll('#cartao .multiselect')].filter(visivel);
                    el = menus.find(m => sem((m.closest('.form-group, .col, .col-md') || m).innerText).includes('bandeira'))
                      || menus.find(m => !conhecidos.includes((m.getAttribute('aria-owns') || '').replace('listbox-', '')));
                  }
                  if (!el) return {erro: 'ausente'};
                  if ((el.className || '').includes('multiselect--disabled')) return {erro: 'desabilitado'};
                  const single = el.querySelector('.multiselect__single');
                  const textoSingle = single ? (single.textContent || '') : '';
                  if (desejados.some(d => casa(textoSingle, d))) return {ok: textoSingle.trim()};
                  let vm = null;
                  let inst = el.__vueParentComponent;
                  for (let i = 0; i < 5 && inst; i++) {
                    const p = inst.proxy || inst.ctx;
                    if (p && typeof p.select === 'function') { vm = p; break; }
                    inst = inst.parent;
                  }
                  const rotulo = o => {
                    if (!o || !vm) return '';
                    if (typeof vm.getOptionLabel === 'function') return String(vm.getOptionLabel(o) || '');
                    return String(o.name || o.label || '');
                  };
                  const atual = vm && vm.internalValue && vm.internalValue[0] ? rotulo(vm.internalValue[0]) : '';
                  if (desejados.some(d => casa(atual, d))) return {ok: atual};
                  const doDom = [...el.querySelectorAll('.multiselect__option')]
                    .map(n => (n.textContent || '').replace(/\\s+/g, ' ').trim())
                    .filter(t => t && !sem(t).includes('nenhum') && !sem(t).includes('carreg') && !sem(t).includes('vazia'));
                  const lista = vm
                    ? ((Array.isArray(vm.options) && vm.options.length ? vm.options : vm.filteredOptions) || [])
                    : [];
                  if (!doDom.length && !lista.length) {
                    if (abrir && vm && typeof vm.activate === 'function' && !el.classList.contains('multiselect--active')) {
                      vm.activate();
                    }
                    return {erro: 'aguardando'};
                  }
                  if (vm) {
                    for (const desejado of desejados) {
                      const opt = lista.find(o => sem(rotulo(o)) === sem(desejado)) || lista.find(o => casa(rotulo(o), desejado));
                      if (!opt) continue;
                      if (!(typeof vm.isSelected === 'function' && vm.isSelected(opt))) vm.select(opt);
                      const novo = vm.internalValue && vm.internalValue[0] ? rotulo(vm.internalValue[0]) : rotulo(opt);
                      if (desejados.some(d => casa(novo, d))) return {ok: novo};
                    }
                  }
                  const item = [...el.querySelectorAll('.multiselect__option')]
                    .find(n => desejados.some(d => casa(n.textContent, d)));
                  if (item) {
                    item.click();
                    const novo = ((el.querySelector('.multiselect__single') || {}).textContent || item.textContent || '').trim();
                    if (desejados.some(d => casa(novo, d))) return {ok: novo};
                  }
                  if (aceitarPrimeira) {
                    const serve = o => {
                      const a = sem(rotulo(o));
                      return a && !a.includes('nenhum') && !a.includes('carreg') && !a.includes('vazia');
                    };
                    const opt = vm ? lista.find(serve) : null;
                    if (opt && vm) {
                      if (!(typeof vm.isSelected === 'function' && vm.isSelected(opt))) vm.select(opt);
                      const novo = vm.internalValue && vm.internalValue[0] ? rotulo(vm.internalValue[0]) : rotulo(opt);
                      if (novo) return {ok: novo};
                    }
                    const dom = [...el.querySelectorAll('.multiselect__option')].find(n => {
                      const a = sem(n.textContent);
                      return a && !a.includes('nenhum') && !a.includes('carreg') && !a.includes('vazia');
                    });
                    if (dom) {
                      dom.click();
                      return {ok: (dom.textContent || '').replace(/\\s+/g, ' ').trim()};
                    }
                  }
                  const vistos = doDom.length ? doDom : lista.map(rotulo).filter(Boolean);
                  return {erro: 'nao achei', vistos: vistos.slice(0, 12)};
                }""",
                [input_id, textos, tentativa == 6, aceitar_primeira],
            )
        except Exception as e:
            raise RuntimeError(f"Não consegui usar o campo {input_id}. {e}") from e
        if not isinstance(lido, dict):
            break
        resultado = lido
        if resultado.get("ok") or resultado.get("erro") in ("desabilitado", "ausente", "nao achei"):
            break
        page.wait_for_timeout(300)
    if resultado.get("ok"):
        return
    if resultado.get("erro") == "desabilitado":
        emit("log", msg=f"{input_id}: campo desabilitado, mantive o valor da tela.")
        return
    if resultado.get("erro") == "ausente":
        raise RuntimeError(f"Não achei o campo {input_id} na tela do cartão.")
    vistos = resultado.get("vistos") or []
    if not vistos:
        raise RuntimeError(f"{input_id} abriu sem opções. Procurei {textos}.")
    raise RuntimeError(f"Não achei {textos} em {input_id}. A tela mostrou: {vistos[:12]}")


_OPCAO = re.compile(
    r"^\s*(?P<cod>\d+)\s*#\s*(?P<nome>.*?)\s*R\$\s*(?P<preco>-?[\d.]+,\d{2})\s*\|\s*Qtde\.:\s*(?P<qtde>-?[\d.]+,\d{2})",
    re.I,
)


def _br_numero(texto: str) -> float:
    return float(texto.strip().replace(".", "").replace(",", "."))


def ler_opcoes_produto(page) -> list[dict]:
    saida = []
    for bruto in page.locator(".multiselect__option:visible").all_inner_texts():
        linha = " ".join(bruto.split())
        m = _OPCAO.match(linha)
        if not m:
            continue
        saida.append(
            {
                "cod": m.group("cod"),
                "nome": m.group("nome").strip(),
                "preco": int(round(_br_numero(m.group("preco")) * 100)),
                "estoque": _br_numero(m.group("qtde")),
                "qtde_txt": m.group("qtde"),
            }
        )
    return saida


def buscar_produto(page, sec, termo: str) -> list[dict]:
    sec.locator(".multiselect").first.click()
    page.wait_for_timeout(150)
    if _visivel(sec.locator(".multiselect__single")):
        page.keyboard.press("Backspace")
        page.wait_for_timeout(100)
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")
    page.keyboard.type(termo, delay=60)
    page.wait_for_timeout(800)
    parar_se_servidor_falhou(page)
    return ler_opcoes_produto(page)


def garantir_quantidade_positiva(page, qtd: int) -> None:
    campo = page.locator("#quantity")
    for _ in range(25):
        if campo.is_enabled():
            break
        page.wait_for_timeout(100)
    atual = (campo.input_value() or "").strip()
    if atual.startswith("-") or _br_numero(atual or "0") != float(qtd):
        digitar_mascara(page, campo, qtd * 100)
        page.wait_for_timeout(200)


def plano_quantidade_desconto(alvo: int, preco: int, estoque: float) -> dict | None:
    """Quantidade até cobrir o valor; o desconto em reais tira a sobra."""
    if preco <= 0 or estoque < 1:
        return None
    qtd = max(1, (alvo + preco - 1) // preco)
    if qtd > int(estoque) or qtd > 40:
        return None
    bruto = qtd * preco
    return {"qtd": qtd, "descontoCentavos": bruto - alvo, "precoCentavos": preco}


def consultar_estoque(page, sec, cod: str, cache: dict) -> dict | None:
    if cod in cache:
        return cache[cod]
    opcoes = buscar_produto(page, sec, cod)
    alvo = next((o for o in opcoes if o["cod"] == cod), None)
    cache[cod] = alvo
    page.keyboard.press("Escape")
    return alvo


def linhas_desejadas(alvo: int) -> int:
    if alvo >= 4500:
        return 4
    if alvo >= 2200:
        return 3
    if alvo >= 1000:
        return 2
    return 1


def _pasta_dados() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    elif sys_platform_darwin():
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    destino = base / "emite_notas"
    destino.mkdir(parents=True, exist_ok=True)
    return destino


def sys_platform_darwin() -> bool:
    return os.uname().sysname == "Darwin" if hasattr(os, "uname") else False


_USADOS: dict[str, list[str]] | None = None


def ncm_ruins() -> set[str]:
    caminho = _pasta_dados() / "produtos_ncm.json"
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except Exception:
        return set()
    if isinstance(dados, list):
        return {str(c) for c in dados if str(c).strip()}
    return set()


def bloquear_ncm(cod: str) -> None:
    cod = str(cod or "").strip()
    if not cod:
        return
    atuais = ncm_ruins()
    if cod in atuais:
        return
    atuais.add(cod)
    try:
        (_pasta_dados() / "produtos_ncm.json").write_text(
            json.dumps(sorted(atuais), ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def _bloquear_item_rejeitado(motivo: str, itens: list | None) -> str:
    """nItem: 1 é o primeiro produto lançado. NCM inválido fica gravado para não repetir."""
    if not itens or "ncm" not in _sem_acento(motivo or ""):
        return ""
    indice = 0
    achado = re.search(r"nItem\s*:\s*(\d+)", motivo or "", re.I)
    if achado:
        indice = max(0, int(achado.group(1)) - 1)
    if indice >= len(itens):
        indice = 0
    cod = str(itens[indice].get("busca") or "").strip()
    bloquear_ncm(cod)
    return cod


def usados_do_valor(alvo: int) -> set[str]:
    global _USADOS
    if _USADOS is None:
        caminho = _pasta_dados() / "produtos_usados.json"
        try:
            _USADOS = json.loads(caminho.read_text(encoding="utf-8"))
        except Exception:
            _USADOS = {}
    return set(_USADOS.get(str(alvo), []))


def lembrar_usados(alvo: int, codigos: list[str]) -> None:
    global _USADOS
    if _USADOS is None:
        usados_do_valor(alvo)
    assert _USADOS is not None
    novos = [c for c in codigos if c]
    chave = str(alvo)
    atual = [c for c in _USADOS.get(chave, []) if c not in novos]
    _USADOS[chave] = (atual + novos)[-16:]
    try:
        (_pasta_dados() / "produtos_usados.json").write_text(
            json.dumps(_USADOS, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def _pesos(candidatos: list[dict], usados_nota: set[str], evitar: set[str], qtds: dict[str, int] | None = None) -> list[float] | None:
    """Porcentagem vira chance. 0 divide o que sobra. Sem nenhuma %, fica a escolha antiga."""
    if not any(int(c.get("frequencia") or 0) > 0 for c in candidatos):
        return None
    marcados = [min(100, int(c.get("frequencia") or 0)) for c in candidatos if int(c.get("frequencia") or 0) > 0]
    soma = sum(marcados)
    fator = 100 / soma if soma > 100 else 1.0
    comuns = [c for c in candidatos if int(c.get("frequencia") or 0) <= 0]
    resto = max(0.0, 100 - soma * fator)
    peso_comum = (resto / len(comuns)) if comuns else 0.0
    pesos: list[float] = []
    for c in candidatos:
        freq = int(c.get("frequencia") or 0)
        peso = freq * fator if freq > 0 else peso_comum
        qtd = 1 if qtds is None else int(qtds.get(c["cod"], 1))
        if freq <= 0 and qtd > 3:
            peso *= 0.05
        elif qtd > 12:
            peso *= 0.2
        if c["cod"] in usados_nota:
            peso *= 0.02
        elif c["cod"] in evitar:
            peso *= 0.25
        pesos.append(peso)
    return pesos


def _sortear(candidatos: list[dict], pesos: list[float]) -> dict:
    total = sum(pesos)
    if total <= 0:
        return candidatos[0]
    tiro = random.random() * total
    acum = 0.0
    for candidato, peso in zip(candidatos, pesos):
        acum += peso
        if tiro <= acum:
            return candidato
    return candidatos[-1]


def _ordem_linha(opcao: dict, restante: int, faltam: int, usados_nota: set[str], evitar: set[str]) -> tuple:
    cod = opcao["cod"]
    preco = int(opcao["preco"])
    cota = max(1, restante // max(faltam, 1))
    penal = 0
    if cod in usados_nota:
        penal += 3
    if cod in evitar:
        penal += 2
    if preco > restante:
        penal += 5
    return (penal, abs(preco - cota), preco)


def _fechar_restante(restante: int, opcoes: list[dict], usados_nota: set[str], evitar: set[str]) -> dict | None:
    viaveis = []
    for opcao in opcoes:
        plano = plano_quantidade_desconto(restante, int(opcao["preco"]), opcao["estoque"])
        if plano is None:
            continue
        viaveis.append((opcao, plano))
    if not viaveis:
        return None
    menor = min(plano["descontoCentavos"] for _, plano in viaveis)
    viaveis = [(opcao, plano) for opcao, plano in viaveis if plano["descontoCentavos"] == menor]
    pesos = _pesos(
        [opcao for opcao, _ in viaveis],
        usados_nota,
        evitar,
        {opcao["cod"]: plano["qtd"] for opcao, plano in viaveis},
    )
    if pesos is not None:
        escolhido = _sortear([opcao for opcao, _ in viaveis], pesos)
        plano = next(p for o, p in viaveis if o["cod"] == escolhido["cod"])
        return {
            "busca": escolhido["cod"],
            "nome": escolhido["nome"],
            "qtd": plano["qtd"],
            "precoCentavos": int(escolhido["preco"]),
            "descontoCentavos": plano["descontoCentavos"],
        }
    melhor = None
    for opcao, plano in viaveis:
        penal = 0
        if opcao["cod"] in usados_nota:
            penal += 3
        if opcao["cod"] in evitar:
            penal += 2
        chave = (
            penal,
            0 if plano["qtd"] <= 3 else plano["qtd"],
            plano["descontoCentavos"],
            plano["qtd"],
        )
        if melhor is None or chave < melhor[0]:
            melhor = (
                chave,
                {
                    "busca": opcao["cod"],
                    "nome": opcao["nome"],
                    "qtd": plano["qtd"],
                    "precoCentavos": int(opcao["preco"]),
                    "descontoCentavos": plano["descontoCentavos"],
                },
            )
    return None if melhor is None else melhor[1]


def montar_linhas(alvo: int, opcoes: list[dict], evitar: set[str] | None = None) -> list[dict] | None:
    """Várias linhas em valor alto. O desconto fica só na última. Evitar repete o valor."""
    evitar = evitar or set()
    vivos = [
        {
            "cod": str(o["cod"]),
            "nome": o.get("nome") or str(o["cod"]),
            "preco": int(o["preco"]),
            "estoque": float(o["estoque"]),
            "frequencia": max(0, min(100, int(o.get("frequencia") or 0))),
        }
        for o in opcoes
        if int(o.get("preco") or 0) > 0 and float(o.get("estoque") or 0) >= 1
    ]
    if not vivos:
        return None
    meta = linhas_desejadas(alvo)
    estoque = {o["cod"]: o["estoque"] for o in vivos}
    linhas: list[dict] = []
    restante = alvo
    usados_nota: set[str] = set()
    while len(linhas) < meta - 1 and restante > 1:
        faltam = meta - len(linhas)
        candidatos = []
        for o in vivos:
            if estoque[o["cod"]] < 1 or o["preco"] >= restante:
                continue
            candidatos.append(o)
        if not candidatos:
            break
        pesos = _pesos(candidatos, usados_nota, evitar)
        if pesos is None:
            candidatos.sort(key=lambda o: _ordem_linha(o, restante, faltam, usados_nota, evitar))
            escolhido = candidatos[0]
        else:
            escolhido = _sortear(candidatos, pesos)
        linhas.append(
            {
                "busca": escolhido["cod"],
                "nome": escolhido["nome"],
                "qtd": 1,
                "precoCentavos": escolhido["preco"],
                "descontoCentavos": 0,
            }
        )
        restante -= escolhido["preco"]
        estoque[escolhido["cod"]] -= 1
        usados_nota.add(escolhido["cod"])
    while True:
        pool = []
        for o in vivos:
            if estoque[o["cod"]] < 1:
                continue
            pool.append({**o, "estoque": estoque[o["cod"]]})
        fecha = _fechar_restante(restante, pool, usados_nota, evitar)
        if fecha is not None:
            linhas.append(fecha)
            return linhas
        if not linhas:
            return None
        ultima = linhas.pop()
        restante += int(ultima["qtd"]) * int(ultima["precoCentavos"]) - int(ultima["descontoCentavos"])
        estoque[ultima["busca"]] += int(ultima["qtd"])
        if ultima["busca"] not in {l["busca"] for l in linhas}:
            usados_nota.discard(ultima["busca"])


def _sobra_para_fechar(preco: int, alvo: int) -> int:
    """Quanto de desconto uma quantidade desse preço deixa para bater o valor."""
    if preco <= 0:
        return 10**9
    resto = alvo % preco
    return 0 if resto == 0 else preco - resto


def _desconto_total(plano: list[dict]) -> int:
    return sum(int(linha.get("descontoCentavos") or 0) for linha in plano)


def resolver_itens(page, sec, nota: dict, catalogo: list) -> list[dict]:
    alvo = int(nota["brutoCentavos"])
    evitar = usados_do_valor(alvo)
    ruins = ncm_ruins()
    freqs: dict[str, int] = {}
    ordem = []
    for p in catalogo or []:
        cod = str(p.get("busca") or "").strip()
        preco = int(p.get("centavos") or 0)
        if not cod or preco <= 0:
            continue
        freq = max(0, min(100, int(p.get("frequencia") or 0)))
        freqs[cod] = freq
        ordem.append((
            _sobra_para_fechar(preco, alvo),
            0 if freq > 0 else 1,
            0 if cod not in evitar else 1,
            -freq,
            preco,
            cod,
        ))
    ordem.sort()
    soma_freq = sum(freqs.values())
    tem_freq = soma_freq > 0
    fav_total = sum(1 for v in freqs.values() if v > 0)
    cache: dict = {}
    pool: list[dict] = []
    melhor: list[dict] | None = None
    fav_consultados = 0
    comuns = 0
    limite = min(len(ordem), 24 if tem_freq else 18)
    avisou_resto = False
    for indice, (*_, cod) in enumerate(ordem):
        if cod in ruins:
            emit("log", msg=f"{cod} ficou de fora: a Sefaz rejeitou o NCM desse produto.")
            continue
        info = consultar_estoque(page, sec, cod, cache)
        if info is not None and int(info["preco"] or 0) <= 0:
            if indice < limite:
                emit("log", msg=f"{cod} {info['nome']} está sem preço no ZWeb e ficou de fora.")
            elif not avisou_resto:
                emit("log", msg="Os primeiros códigos não fecham a nota. Procurando o restante do catálogo.")
                avisou_resto = True
            continue
        if info is None or float(info["estoque"]) < 1:
            if indice < limite:
                emit(
                    "log",
                    msg=(
                        f"{cod} sem estoque bastante no ZWeb."
                        if info is None
                        else f"{cod} {info['nome']} está com quantidade {info['qtde_txt']} no ZWeb."
                    ),
                )
            elif not avisou_resto:
                emit("log", msg="Os primeiros códigos não fecham a nota. Procurando o restante do catálogo.")
                avisou_resto = True
            continue
        if freqs.get(cod, 0) > 0:
            fav_consultados += 1
        item = dict(info)
        item["frequencia"] = freqs.get(cod, 0)
        pool.append(item)
        if item["frequencia"] <= 0:
            comuns += 1
        plano = montar_linhas(alvo, pool, evitar)
        if plano is None:
            continue
        if melhor is None or _desconto_total(plano) < _desconto_total(melhor):
            melhor = plano
        if _desconto_total(plano) == 0:
            break
        if indice >= limite and melhor is not None and _desconto_total(melhor) <= 30:
            break
        if tem_freq and fav_consultados >= min(fav_total, 8) and (soma_freq >= 100 or comuns >= 6):
            if melhor is not None and _desconto_total(melhor) == 0:
                break
            continue
    plano = melhor
    if not plano:
        raise RuntimeError(
            f"Não achei produtos com estoque para fechar {centavos_campo(alvo)}."
        )
    for linha in plano:
        emit(
            "log",
            msg=(
                f"{linha['qtd']}× {linha['busca']} {linha['nome']} a {centavos_campo(linha['precoCentavos'])}, "
                f"desconto {centavos_campo(linha['descontoCentavos'])}."
            ),
        )
    lembrar_usados(alvo, [linha["busca"] for linha in plano])
    return plano


def aplicar_desconto(page, sec, centavos: int) -> None:
    if centavos <= 0:
        return
    reais = sec.locator("button").filter(has_text=re.compile(r"^R\$$"))
    if reais.count():
        reais.first.click()
        page.wait_for_timeout(150)
    campo = page.locator('[id="productForm.discount"]')
    digitar_mascara(page, campo, centavos)
    page.keyboard.press("Tab")
    page.wait_for_timeout(200)


def vigiar_avisos(page) -> None:
    """Guarda o texto do aviso que some no canto, antes de ele desaparecer."""
    try:
        page.evaluate(
            """() => {
              if (window.__avisosZweb) return;
              window.__avisosZweb = [];
              const guardar = () => {
                document.querySelectorAll('.v-toast__text, .swal2-toast').forEach(el => {
                  const texto = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                  if (texto && !window.__avisosZweb.includes(texto)) window.__avisosZweb.push(texto);
                });
              };
              new MutationObserver(guardar).observe(document.body, {childList: true, subtree: true, characterData: true});
              guardar();
            }"""
        )
    except Exception:
        return


_FALHA_SERVIDOR = re.compile(
    r"internal server error|erro interno|server error|erro no servidor",
    re.I,
)


def _falha_de_servidor(page) -> str:
    """O ZWeb mostra Internal Server Error num aviso. Seguir preenchendo grava a nota errada."""
    try:
        achado = page.evaluate(
            """() => {
              const limpo = s => (s || '').replace(/\\s+/g, ' ').trim();
              const avisos = [...(window.__avisosZweb || [])];
              document.querySelectorAll(
                '.v-toast__text, .swal2-toast, .swal2-title, .swal2-html-container'
              ).forEach(el => {
                const t = limpo(el.innerText || el.textContent);
                if (t) avisos.push(t);
              });
              const corpo = limpo(document.body ? document.body.innerText : '').slice(0, 280);
              return {avisos, corpo};
            }"""
        )
    except Exception:
        return ""
    if not isinstance(achado, dict):
        return ""
    vistos = []
    for item in achado.get("avisos") or []:
        texto = " ".join(str(item).split())
        if texto:
            vistos.append(texto)
    corpo = " ".join(str(achado.get("corpo") or "").split())
    if corpo:
        vistos.append(corpo)
    for texto in vistos:
        if _FALHA_SERVIDOR.search(texto):
            return texto[:180]
    return ""


def parar_se_servidor_falhou(page) -> None:
    texto = _falha_de_servidor(page)
    if not texto:
        return
    emit("log", msg=f"Aviso do ZWeb: {texto}")
    raise RuntimeError(
        "O ZWeb respondeu com erro interno. Parei o preenchimento e não cliquei em Transmitir."
    )


def descarregar_avisos(page) -> None:
    try:
        novos = page.evaluate(
            """() => {
              const lista = window.__avisosZweb || [];
              window.__avisosZweb = [];
              return lista;
            }"""
        )
    except Exception:
        parar_se_servidor_falhou(page)
        return
    grave = False
    if isinstance(novos, list):
        for texto in novos:
            limpo = " ".join(str(texto).split())
            if not limpo:
                continue
            emit("log", msg=f"Aviso do ZWeb: {limpo}")
            if _FALHA_SERVIDOR.search(limpo):
                grave = True
    if grave:
        raise RuntimeError(
            "O ZWeb respondeu com erro interno. Parei o preenchimento e não cliquei em Transmitir."
        )
    parar_se_servidor_falhou(page)


def lancar_produto(page, item: dict, catalogo: list | None = None) -> None:
    codigo = str(item.get("busca") or "").strip()
    nome = str(item.get("nome") or codigo)
    qtd = int(item.get("qtd") or 1)
    desconto = int(item.get("descontoCentavos") or 0)
    emit("log", msg=f"Lançando {qtd}× {codigo} {nome}, desconto {centavos_campo(desconto)}")
    sec = page.locator("#fiscalNFCeProductsData")
    buscar_produto(page, sec, codigo)
    page.locator(".multiselect__option").filter(
        has_text=re.compile(rf"^\s*{re.escape(codigo)}\s*#")
    ).first.click()
    page.wait_for_timeout(300)
    garantir_quantidade_positiva(page, qtd)
    aplicar_desconto(page, sec, desconto)
    botao = _botao_incluir(sec).first
    pronto = False
    for _ in range(16):
        parar_se_servidor_falhou(page)
        try:
            if botao.count() and botao.is_visible() and botao.is_enabled(timeout=500):
                pronto = True
                break
        except Exception:
            pass
        page.wait_for_timeout(250)
    if not pronto:
        parar_se_servidor_falhou(page)
        raise RuntimeError(
            f"O botão de incluir {codigo} {nome} não apareceu. "
            "Parei o preenchimento e não cliquei em Transmitir."
        )
    clicar_icone_incluir(page, sec)
    page.wait_for_timeout(500)
    descarregar_avisos(page)


def _clicar_incluir(page) -> bool:
    for sel in (
        "button[title*='Adicionar' i]",
        "button[title*='Incluir' i]",
        "button[aria-label*='Adicionar' i]",
        "button[aria-label*='Incluir' i]",
        "a[title*='Adicionar' i]",
    ):
        botao = page.locator(sel)
        if not _visivel(botao):
            continue
        texto = (botao.first.inner_text() or "").strip()
        if PROIBIDO.search(texto):
            continue
        botao.first.click()
        return True
    return False


def abrir_pagamento(page) -> None:
    cartao = page.get_by_text(re.compile(r"4\s*Cartão"))
    if _visivel(cartao):
        return
    page.get_by_text("Formas de pagamento").first.scroll_into_view_if_needed()
    page.get_by_text("Formas de pagamento").first.click()
    page.get_by_text(re.compile(r"4\s*Cartão")).first.wait_for(timeout=8000)


def opcoes_parcelamento(tipo: str) -> list[str]:
    t = (tipo or "").lower()
    if "débito" in t or "debito" in t:
        return ["Débito à vista"]
    if "crédito" in t or "credito" in t:
        return ["Crédito à vista", "Cartão de crédito à vista"]
    return ["Débito à vista"]


def opcoes_bandeira(bandeira: str) -> list[str]:
    n = (bandeira or "").strip().lower()
    if n == "elo":
        return ["ELO"]
    if "maestro" in n or "master" in n:
        return ["MasterCard"]
    if n == "visa":
        return ["Visa"]
    if "hiper" in n:
        return ["Hipercard"]
    if "amex" in n or "american" in n:
        return ["American Express"]
    return [bandeira] if bandeira else []


def _botao_forma(page, rotulo: str):
    return page.locator("#fiscalNFCePayment span.btn-payment").filter(
        has_text=re.compile(rf"^\s*\d+\s+{re.escape(rotulo)}\s*$", re.I),
        visible=True,
    ).first


def _forma_ativa(botao) -> bool:
    return "active" in ((botao.get_attribute("class") or "").split())


def ativar_forma(page, rotulo: str):
    """O ZWeb abre o pagamento em Dinheiro e só mostra o painel da forma
    cujo botão está com a classe active. O clique do mouse não troca isso
    quando a janela está pela metade."""
    botao = _botao_forma(page, rotulo)
    botao.wait_for(state="visible", timeout=8000)
    campo = botao.locator("xpath=ancestor::tr[1]").locator("input.input-payment").first
    for _ in range(3):
        botao.evaluate("el => el.click()")
        page.wait_for_timeout(200)
        if _forma_ativa(botao):
            return botao
        if campo.count():
            campo.evaluate("el => el.click()")
            page.wait_for_timeout(200)
        if _forma_ativa(botao):
            return botao
    raise RuntimeError(f"A forma {rotulo} não ficou selecionada no ZWeb.")


def digitar_valor_forma(page, botao, centavos: int) -> None:
    campo = botao.locator("xpath=ancestor::tr[1]").locator("input.input-payment").first
    esperado = centavos_campo(centavos)
    campo.focus()
    page.wait_for_timeout(300)
    if _mesmo_total(campo.input_value() or "", esperado):
        campo.evaluate("el => el.blur()")
        return
    visto = ""
    for _ in range(2):
        campo.focus()
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        page.wait_for_timeout(80)
        page.keyboard.type(str(abs(int(centavos))), delay=30)
        page.wait_for_timeout(120)
        campo.evaluate("el => el.blur()")
        page.wait_for_timeout(150)
        visto = (campo.input_value() or "").strip()
        if _mesmo_total(visto, esperado):
            return
    raise RuntimeError(f"O valor do cartão ficou {visto or 'vazio'}, e o esperado é {esperado}.")


def _focar_codigo_transacao(page, painel_id: str) -> dict:
    """O código fica no campo de 128 caracteres do painel, sem clicar na tela."""
    lido = page.evaluate(
        """(painelId) => {
          const painel = document.getElementById(painelId);
          if (!painel) return {erro: 'painel'};
          const caixa = painel.getBoundingClientRect();
          if (caixa.width <= 0 || caixa.height <= 0) return {erro: 'painel'};
          let input = [...painel.querySelectorAll('input[maxlength="128"], textarea[maxlength="128"]')]
            .find(el => !el.classList.contains('multiselect__input'));
          if (!input) {
            const sem = s => (s || '').normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').toLowerCase();
            const label = [...painel.querySelectorAll('label, span, div, p')].find(el => {
              const texto = sem(el.textContent);
              if (!texto.includes('numero da transacao')) return false;
              return ![...el.children].some(c => sem(c.textContent).includes('numero da transacao'));
            });
            input = label && label.parentElement && label.parentElement.querySelector('input, textarea');
          }
          if (!input || input.classList.contains('multiselect__input')) return {erro: 'sem campo'};
          if (input.disabled) return {erro: 'desabilitado'};
          input.focus();
          input.select();
          return {ok: true};
        }""",
        painel_id,
    )
    return lido if isinstance(lido, dict) else {"erro": "painel"}


def _ler_codigo_transacao(page, painel_id: str) -> str:
    lido = page.evaluate(
        """(painelId) => {
          const painel = document.getElementById(painelId);
          if (!painel) return '';
          let input = [...painel.querySelectorAll('input[maxlength="128"], textarea[maxlength="128"]')]
            .find(el => !el.classList.contains('multiselect__input'));
          if (!input) {
            const sem = s => (s || '').normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').toLowerCase();
            const label = [...painel.querySelectorAll('label, span, div, p')].find(el => {
              const texto = sem(el.textContent);
              if (!texto.includes('numero da transacao')) return false;
              return ![...el.children].some(c => sem(c.textContent).includes('numero da transacao'));
            });
            input = label && label.parentElement && label.parentElement.querySelector('input, textarea');
          }
          if (!input) return '';
          input.blur();
          return String(input.value || '');
        }""",
        painel_id,
    )
    return lido if isinstance(lido, str) else ""


def digitar_codigo_transacao(page, painel_id: str, codigo: str, rotulo: str = "pagamento") -> None:
    codigo = str(codigo or "").strip()
    if not codigo:
        return
    visto = ""
    for _ in range(2):
        fechar_menus(page)
        estado: dict = {}
        for _ in range(8):
            estado = _focar_codigo_transacao(page, painel_id)
            if estado.get("ok"):
                break
            if estado.get("erro") != "sem campo":
                break
            page.wait_for_timeout(300)
        if estado.get("erro") == "desabilitado":
            raise RuntimeError(
                f"O Número da transação do {rotulo} está desabilitado. O valor não ficou nesse pagamento."
            )
        if not estado.get("ok"):
            raise RuntimeError(f"Não achei o Número da transação na tela do {rotulo}.")
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        page.keyboard.type(codigo, delay=15)
        visto = _ler_codigo_transacao(page, painel_id)
        if visto == codigo:
            return
        page.wait_for_timeout(200)
    raise RuntimeError(f"O código ficou {visto or 'vazio'}, e o esperado é {codigo}.")


def preencher_pix(page, nota: dict) -> None:
    centavos = int(nota["brutoCentavos"])
    valor = centavos_campo(centavos)
    codigo = str(nota.get("autorizacao") or "")
    emit("log", msg=f"PIX {valor} · aut. {codigo}")
    abrir_secao(page, "heading_fiscalNFCePayment")

    def preparar() -> None:
        botao = ativar_forma(page, "PIX")
        digitar_valor_forma(page, botao, centavos)
        try:
            page.locator("#pix").wait_for(state="visible", timeout=8000)
        except PwTimeout:
            raise RuntimeError(
                "O PIX não abriu a conta de destino. O valor ficou em Dinheiro."
            )
        try:
            escolher_multiselect(page, "category", ["Receita de Vendas"])
        except Exception as e:
            emit("log", msg=f"category: mantive o que já estava na tela ({e})")
        if not _forma_ativa(botao):
            botao = ativar_forma(page, "PIX")
            digitar_valor_forma(page, botao, centavos)
        escolher_multiselect(
            page,
            "paymentMode",
            ["PIX", "Pagamento Instantâneo", "Pix dinâmico", "Pix estático"],
            aceitar_primeira=True,
        )
        if _forma_ativa(_botao_forma(page, "Dinheiro")):
            raise RuntimeError(
                "O valor voltou para Dinheiro e o saldo ficou vermelho. O PIX não foi incluído."
            )
        digitar_codigo_transacao(page, "pix", codigo, "PIX")

    preparar()
    clicar_icone_incluir(page, page.locator("#pix"))
    confirmar_pagamento_incluido(
        page, page.locator("#pix"), forma="PIX", refazer=preparar
    )


def _painel_aberto(page, painel_id: str) -> bool:
    try:
        return page.locator(f"#{painel_id}").is_visible()
    except Exception:
        return False


def reabrir_cartao(page, nota: dict):
    """O valor volta para Dinheiro e o painel do cartão some. Abre de novo."""
    emit("log", msg="O cartão fechou. Abrindo de novo.")
    botao = ativar_forma(page, "Cartão")
    digitar_valor_forma(page, botao, int(nota["brutoCentavos"]))
    page.locator("#cartao").wait_for(state="visible", timeout=8000)
    for campo, opcoes in (
        ("finishingMode", ["Venda SmartPOS"]),
        ("financeCardAdministrator", ["Caixa"]),
        ("category", ["Receita de Vendas"]),
    ):
        try:
            escolher_multiselect(page, campo, opcoes)
        except Exception as e:
            emit("log", msg=f"{campo}: mantive o que já estava na tela ({e})")
    escolher_multiselect(page, "paymentMode", opcoes_parcelamento(nota.get("tipo") or ""))
    return botao


def preencher_pagamento(page, nota: dict) -> None:
    parar_se_servidor_falhou(page)
    if str(nota.get("forma") or "").strip().upper() == "PIX":
        preencher_pix(page, nota)
        return
    valor = centavos_campo(int(nota["brutoCentavos"]))
    emit(
        "log",
        msg=f"Cartão {valor} · {nota.get('tipo')} · {nota.get('bandeira')} · aut. {nota.get('autorizacao')}",
    )
    abrir_secao(page, "heading_fiscalNFCePayment")

    def preparar() -> None:
        botao = ativar_forma(page, "Cartão")
        digitar_valor_forma(page, botao, int(nota["brutoCentavos"]))
        if not _forma_ativa(botao):
            botao = ativar_forma(page, "Cartão")
            digitar_valor_forma(page, botao, int(nota["brutoCentavos"]))
        try:
            page.locator("#cartao").wait_for(state="visible", timeout=8000)
        except PwTimeout:
            raise RuntimeError(
                "O cartão não abriu parcelamento e bandeira. O valor ficou em Dinheiro."
            )
        for campo, opcoes in (
            ("finishingMode", ["Venda SmartPOS"]),
            ("financeCardAdministrator", ["Caixa"]),
            ("category", ["Receita de Vendas"]),
        ):
            try:
                escolher_multiselect(page, campo, opcoes)
            except Exception as e:
                emit("log", msg=f"{campo}: mantive o que já estava na tela ({e})")
        if not _forma_ativa(botao):
            emit("log", msg="O valor voltou para Dinheiro. Selecionando o cartão de novo.")
            botao = ativar_forma(page, "Cartão")
            digitar_valor_forma(page, botao, int(nota["brutoCentavos"]))
        try:
            escolher_multiselect(page, "paymentMode", opcoes_parcelamento(nota.get("tipo") or ""))
        except RuntimeError:
            if not _forma_ativa(botao):
                botao = ativar_forma(page, "Cartão")
                digitar_valor_forma(page, botao, int(nota["brutoCentavos"]))
            escolher_multiselect(page, "paymentMode", opcoes_parcelamento(nota.get("tipo") or ""))
        if not _painel_aberto(page, "cartao"):
            reabrir_cartao(page, nota)
        bandeiras = opcoes_bandeira(nota.get("bandeira") or "")
        if bandeiras:
            try:
                escolher_multiselect(page, "financeFlag", bandeiras)
            except RuntimeError:
                if not _painel_aberto(page, "cartao"):
                    reabrir_cartao(page, nota)
                escolher_multiselect(page, "financeFlag", bandeiras)
        if not _painel_aberto(page, "cartao"):
            reabrir_cartao(page, nota)
        campo_valor = _botao_forma(page, "Cartão").locator("xpath=ancestor::tr[1]").locator(
            "input.input-payment"
        ).first
        if not _mesmo_total(campo_valor.input_value() or "", valor):
            digitar_valor_forma(page, _botao_forma(page, "Cartão"), int(nota["brutoCentavos"]))
        digitar_codigo_transacao(page, "cartao", str(nota.get("autorizacao") or ""), "cartão")

    preparar()
    clicar_icone_incluir(page, page.locator("#cartao"))
    confirmar_pagamento_incluido(
        page, page.locator("#cartao"), forma="Cartão", refazer=preparar
    )


def _texto_dialogo(page) -> str:
    try:
        texto = page.evaluate(
            """() => {
              const els = [...document.querySelectorAll('.swal2-popup, .modal.show, [role=dialog]')];
              return els
                .map(el => (el.innerText || '').replace(/\\s+/g, ' ').trim())
                .filter(Boolean)
                .join(' | ')
                .slice(0, 300);
            }"""
        )
    except Exception:
        return ""
    return texto if isinstance(texto, str) else ""


def clicar_botao_transmitir(page) -> None:
    """O Transmitir fica no rodapé. Um aviso aberto esconde o botão da busca por papel."""
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    fechar_avisos(page)
    aviso = _texto_dialogo(page)
    if aviso:
        raise RuntimeError(f"O ZWeb abriu um aviso e o Transmitir ficou sem clique: {aviso}")
    botao = page.locator("footer.footer-form button, footer button").filter(
        has_text=re.compile(r"^\s*Transmitir\s*$")
    )
    ultimo = ""
    for _ in range(8):
        if botao.count() == 0:
            page.wait_for_timeout(400)
            continue
        alvo = botao.first
        try:
            if not alvo.is_enabled():
                page.wait_for_timeout(400)
                continue
            alvo.scroll_into_view_if_needed(timeout=2000)
            alvo.click(timeout=2500)
            return
        except Exception as e:
            ultimo = str(e).split("\n", 1)[0]
            try:
                alvo.evaluate("el => el.click()")
                return
            except Exception as e2:
                ultimo = str(e2).split("\n", 1)[0]
                page.wait_for_timeout(400)
    if botao.count() == 0:
        try:
            nomes = page.locator("footer button").all_inner_texts()
        except Exception:
            nomes = []
        vistos = [" ".join(n.split()) for n in nomes if n.strip()]
        raise RuntimeError(
            "O botão Transmitir não estava na tela."
            + (f" O rodapé mostrou: {vistos}." if vistos else " O rodapé não tinha botões.")
        )
    raise RuntimeError(f"Não consegui clicar em Transmitir. {ultimo}")


def preencher_nota(page, nota: dict, clicar_transmitir: bool, catalogo: list | None = None) -> None:
    anteriores = nova_nfce(page)
    vigiar_avisos(page)
    sec = page.locator("#fiscalNFCeProductsData")
    itens = resolver_itens(page, sec, nota, catalogo or [])
    for item in itens:
        lancar_produto(page, item, catalogo)
    preencher_pagamento(page, nota)
    if clicar_transmitir:
        emit("log", msg="Clicando em Transmitir")
        danfes_antes = danfes_abertos(page)
        clicar_botao_transmitir(page)
        if not _esperar_lista_ou_danfe(page):
            emit("log", msg="A lista não voltou sozinha. Abrindo de novo.")
            page.goto("https://zweb.com.br/#/fiscal/nfce", wait_until="domcontentloaded")
        esperar_nota_na_lista(page, nota, anteriores, danfes_antes, itens)
        descarregar_avisos(page)
        return
    emit("log", msg="Nota preenchida na tela. Transmitir não foi clicado.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fila", required=True)
    ap.add_argument("--porta", type=int, default=9222)
    ap.add_argument("--so-preencher", action="store_true")
    ap.add_argument("--pausa", type=float, default=0)
    args = ap.parse_args()

    fila = json.loads(Path(args.fila).read_text(encoding="utf-8"))
    notas = list(fila.get("notas") or [])
    if not notas:
        emit("erro", msg="Nenhuma nota pronta para o ZWeb.")
        raise SystemExit(1)
    if args.so_preencher and len(notas) > 1:
        emit(
            "log",
            msg="Sem Transmitir, preencho só a primeira nota e paro na tela para conferir.",
        )
        notas = notas[:1]

    emit("log", msg=f"{len(notas)} nota(s). Conectando no Chrome…")

    with sync_playwright() as p:
        browser = conectar(p, args.porta)
        page = pagina_zweb(browser)
        emit("log", msg=f"ZWeb em {page.url}")
        if not sessao_zweb(page):
            emit(
                "erro",
                msg=(
                    "Entre no ZWeb nesta janela do Chrome que acabou de abrir "
                    "e aperte Preencher de novo. "
                    "O Firefox que já estava aberto não é controlado pelo aplicativo."
                ),
            )
            raise SystemExit(5)

        recolher_recibos(page)

        for i, nota in enumerate(notas, 1):
            comp = nota["comprovante"]
            emit(
                "andamento",
                atual=i,
                total=len(notas),
                comprovante=comp,
                msg=f"Preenchendo {i} de {len(notas)}",
            )
            try:
                preencher_nota(
                    page,
                    nota,
                    clicar_transmitir=not args.so_preencher,
                    catalogo=fila.get("catalogo") or [],
                )
            except (PwTimeout, Exception) as e:
                emit("erro", comprovante=comp, msg=f"Parou na nota {comp}: {e}")
                raise SystemExit(4)
            evento = "preenchida" if args.so_preencher else "transmitida"
            emit(
                evento,
                comprovante=comp,
                msg="Nota preenchida, sem transmitir." if args.so_preencher else f"Nota {comp} enviada no ZWeb.",
            )
            if i < len(notas) and args.pausa > 0:
                emit("log", msg=f"Pausa de {args.pausa:.0f}s pedida antes da próxima nota…")
                time.sleep(args.pausa)

    emit("fim", msg="Fila concluída.")


if __name__ == "__main__":
    main()
