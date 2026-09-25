# -*- coding: utf-8 -*-
"""Armazenamento de arquivos do sistema em `instance/arquivos/`.

Fora de `static`: o download sempre passa por uma rota autenticada. Os nomes
gravados são gerados (uuid) — o nome original fica só no banco.
"""
import secrets
import shutil
from pathlib import Path

from flask import current_app
from werkzeug.utils import secure_filename


def raiz() -> Path:
    pasta = Path(current_app.instance_path) / "arquivos"
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta


def caminho_absoluto(relativo: str) -> Path:
    """Resolve um caminho gravado no banco, sem permitir sair da raiz."""
    base = raiz().resolve()
    alvo = (base / relativo).resolve()
    if base not in alvo.parents:
        raise ValueError("Caminho de arquivo inválido.")
    return alvo


def _destino(subpasta: str, nome_original: str) -> tuple[Path, str]:
    nome = secure_filename(nome_original) or "arquivo"
    relativo = f"{subpasta}/{secrets.token_hex(8)}_{nome}"
    destino = raiz() / relativo
    destino.parent.mkdir(parents=True, exist_ok=True)
    return destino, relativo


def guardar_bytes(conteudo: bytes, subpasta: str, nome_original: str) -> tuple[str, int]:
    destino, relativo = _destino(subpasta, nome_original)
    destino.write_bytes(conteudo)
    return relativo, len(conteudo)


def guardar_arquivo(origem: Path, subpasta: str, nome_original: str) -> tuple[str, int]:
    destino, relativo = _destino(subpasta, nome_original)
    shutil.copyfile(origem, destino)
    return relativo, destino.stat().st_size


def temporario(sufixo: str) -> tuple[str, Path]:
    """Arquivo temporário identificado por token (ex.: prévia do razão)."""
    token = secrets.token_hex(16)
    pasta = raiz() / "tmp"
    pasta.mkdir(parents=True, exist_ok=True)
    return token, pasta / f"{token}{sufixo}"


def temporario_existente(token: str, sufixo: str) -> Path | None:
    if not token or len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
        return None
    caminho = raiz() / "tmp" / f"{token}{sufixo}"
    return caminho if caminho.exists() else None
