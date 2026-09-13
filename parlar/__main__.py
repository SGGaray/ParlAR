"""Punto de entrada: python -m parlar [flags]

Los flags están en español; los flags en inglés también se aceptan
como alias, por si preferís esa nomenclatura.
"""

import argparse
import signal
import sys

from .config import Config, ErrorConfiguracion

_ALIAS_MODO = {"frase": "utterance"}
_ALIAS_REESCRITURA = {"ninguna": "none", "conciso": "concise", "correo": "email"}


def _interrumpir_por_sigterm(_numero, _frame):
    """Convierte SIGTERM en la interrupción ordenada que App ya maneja."""
    raise KeyboardInterrupt


def _ejecutar_con_sigterm(app):
    """Instala SIGTERM solo durante la ejecución del entry point."""
    handler_anterior = signal.signal(
        signal.SIGTERM, _interrumpir_por_sigterm)
    try:
        return app.ejecutar()
    finally:
        signal.signal(signal.SIGTERM, handler_anterior)


def _crear_parser(cfg: Config) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="parlar",
        description="ParlAR: dictado local a nivel sistema para Linux. "
                    "Sin telemetría; la red solo se usa si configurás un "
                    "servicio externo, como Ollama remoto.")
    ap.add_argument("--modelo", "--model", dest="modelo", default=cfg.model_size,
                    help="tiny|base|small|medium|large-v3 (por defecto: %(default)s)")
    ap.add_argument("--dispositivo", "--device", dest="dispositivo", default=cfg.device,
                    help="auto|cpu|cuda")
    ap.add_argument("--idioma", "--language", dest="idioma", default=cfg.language,
                    help="código ISO, ej. es, en. Vacío = autodetectar "
                         "(por defecto: %(default)s)")
    ap.add_argument("--modo", "--mode", dest="modo", default=cfg.mode,
                    choices=sorted(Config.MODOS | {"frase"}),
                    help="frase (=utterance) o streaming")
    ap.add_argument("--reescritura", "--rewrite", dest="reescritura",
                    default=cfg.rewrite_mode,
                    choices=sorted(Config.REESCRITURAS |
                                   {"ninguna", "conciso", "correo"}))
    ap.add_argument("--inyector", "--injector", dest="inyector", default=cfg.injector,
                    choices=sorted(Config.INYECTORES))
    ap.add_argument("--sin-indicador", "--no-overlay", dest="sin_indicador",
                    action="store_true", help="corre sin el punto indicador")
    ap.add_argument("--guionar", "--guionar-enabled", dest="guionar",
                    action="store_true", default=cfg.guionar,
                    help="envía texto y estado VAD al teleprompter GuionAR")
    ap.add_argument("--guionar-socket", dest="guionar_socket",
                    default=cfg.guionar_socket,
                    help="ruta del socket de GuionAR "
                         "(por defecto: $XDG_RUNTIME_DIR/guionar.sock)")
    ap.add_argument("--guardar-sesion", "--save-session", dest="guardar_sesion",
                    action="store_true", default=cfg.guardar_sesion,
                    help="guarda cada texto confirmado en "
                         "~/.local/share/parlar/sesiones/ (archivo exclusivo) "
                         "(apagado por defecto, ver SECURITY.md)")
    ap.add_argument("--guardar-config", "--save-config", dest="guardar_config",
                    action="store_true",
                    help="persiste los flags actuales en ~/.config/parlar/config.json")
    return ap


def main():
    if any(argumento in {"-h", "--help"} for argumento in sys.argv[1:]):
        _crear_parser(Config()).parse_args()

    try:
        cfg = Config.load()
    except ErrorConfiguracion as e:
        print(f"[config] configuración inválida: {e}", file=sys.stderr)
        raise SystemExit(2)
    ap = _crear_parser(cfg)
    args = ap.parse_args()

    cfg.model_size = args.modelo
    cfg.device = args.dispositivo
    cfg.language = args.idioma
    cfg.mode = _ALIAS_MODO.get(args.modo, args.modo)
    cfg.rewrite_mode = _ALIAS_REESCRITURA.get(args.reescritura, args.reescritura)
    cfg.injector = args.inyector
    if args.sin_indicador:
        cfg.overlay = False
    cfg.guionar = args.guionar
    cfg.guionar_socket = args.guionar_socket
    cfg.guardar_sesion = args.guardar_sesion
    try:
        cfg.validate()
    except ErrorConfiguracion as e:
        print(f"[config] configuración inválida: {e}", file=sys.stderr)
        raise SystemExit(2)
    if args.guardar_config:
        try:
            cfg.save()
        except OSError as e:
            print(f"[config] no se pudo guardar: {e}", file=sys.stderr)
            raise SystemExit(2)
        print("[config] guardada")

    from .app import App  # imports pesados diferidos hasta después del parseo
    _ejecutar_con_sigterm(App(cfg))


if __name__ == "__main__":
    main()
