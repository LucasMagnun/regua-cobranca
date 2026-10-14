"""Monta o e-mail de cada marco a partir dos templates em templates/."""

from __future__ import annotations

import html
import re
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from .config import Config, Marco
from .email_provedores import Email

PASTA_TEMPLATES = Path(__file__).resolve().parent / "templates"

_env = Environment(
    loader=FileSystemLoader(PASTA_TEMPLATES),
    autoescape=select_autoescape(["html"]),
    undefined=StrictUndefined,  # variável faltando no template = erro, não texto vazio
)


def brl(valor: float) -> str:
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def data_br(valor) -> str:
    if isinstance(valor, str):
        valor = date.fromisoformat(valor[:10])
    return valor.strftime("%d/%m/%Y")


_env.filters["brl"] = brl
_env.filters["data_br"] = data_br


def html_para_texto(conteudo: str) -> str:
    """Versão em texto puro (melhora entrega e é exibida por clientes sem HTML)."""
    t = re.sub(r"(?is)<(style|script|head).*?</\1>", "", conteudo)
    t = re.sub(r"\s+", " ", t)                                    # quebras do template não contam
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r"(?i)</p>|</h\d>|</li>", "\n\n", t)
    t = re.sub(r"(?i)</tr>", "\n", t)
    t = re.sub(r"(?i)</td>", " | ", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    linhas = [re.sub(r"\s*\|\s*$", "", ln).strip() for ln in t.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(linhas)).strip()


class BoletoIndisponivel(Exception):
    """Os textos aprovados citam um boleto com vencimento; sem ele, não envia em produção."""


def montar(cfg: Config, marco: Marco, candidato: dict, para_email: str) -> Email:
    nome = str(candidato["nome"] or "").strip()
    boleto = candidato.get("boleto") or None   # {'vencimento': date, 'link': str|None, 'linha_digitavel': str|None}
    if boleto is None and not cfg.em_teste:
        raise BoletoIndisponivel("Boleto para pagamento não disponível para este beneficiário")

    contexto = {
        "nome": nome,
        "dias_atraso": candidato["dias_atraso"],
        "valor_total": candidato["valor_total"],
        "qtd_cobrancas": candidato["qtd_cobrancas"],
        "itens": candidato["itens"],
        "boleto": boleto,
        "email_atendimento": cfg.email_atendimento,
        "teste": cfg.em_teste,
        "email_original": candidato["email"] if isinstance(candidato["email"], str) else "(sem e-mail)",
    }
    corpo = _env.get_template(marco.template).render(**contexto)
    assunto = f"[TESTE] {marco.assunto}" if cfg.em_teste else marco.assunto
    return Email(
        para_email=para_email,
        para_nome=nome,
        assunto=assunto,
        html=corpo,
        texto=html_para_texto(corpo),
        tags=[marco.tipo],
    )
