"""Proceso auxiliar cancelable para una única request HTTP(S) de Ollama."""

import sys
import time
import urllib.request

from .procesador_texto import _leer_json_ollama


def main() -> int:
    if len(sys.argv) != 2:
        return 2

    try:
        timeout = float(sys.argv[1])
        if timeout <= 0:
            return 2

        entrada = sys.stdin.buffer.read()
        url_bytes, separador, cuerpo = entrada.partition(b"\n")
        if not separador or not url_bytes:
            return 2

        url = url_bytes.decode("utf-8")

        request = urllib.request.Request(
            url,
            data=cuerpo,
            headers={"Content-Type": "application/json"},
        )

        deadline = time.monotonic() + timeout
        with urllib.request.urlopen(request, timeout=timeout) as respuesta:
            data = _leer_json_ollama(respuesta, deadline)

        texto = data.get("response", "")
        if not isinstance(texto, str):
            return 1

        sys.stdout.buffer.write(texto.encode("utf-8"))
        return 0

    except BaseException:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
