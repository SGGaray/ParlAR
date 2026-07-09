"""Punto de entrada: python -m parlar [flags]

Los flags están en español; los flags en inglés también se aceptan
como alias, por si preferís esa nomenclatura.
"""

import argparse

from .config import Config

_ALIAS_MODO = {"frase": "utterance"}
_ALIAS_REESCRITURA = {"ninguna": "none", "conciso": "concise", "correo": "email"}


def main():
    cfg = Config.load()
    ap = argparse.ArgumentParser(
        prog="parlar",
        description="ParlAR: dictado local a nivel sistema para Linux. "
                    "Nada sale de tu máquina.")
    ap.add_argument("--modelo", "--model", dest="modelo", default=cfg.model_size,
                    help="tiny|base|small|medium|large-v3 (por defecto: %(default)s)")
    ap.add_argument("--dispositivo", "--device", dest="dispositivo", default=cfg.device,
                    help="auto|cpu|cuda")
    ap.add_argument("--idioma", "--language", dest="idioma", default=cfg.language,
                    help="código ISO, ej. es, en. Vacío = autodetectar "
                         "(por defecto: %(default)s)")
    ap.add_argument("--modo", "--mode", dest="modo", default=cfg.mode,
                    choices=["utterance", "frase", "streaming"],
                    help="frase (=utterance) o streaming")
    ap.add_argument("--reescritura", "--rewrite", dest="reescritura",
                    default=cfg.rewrite_mode,
                    choices=["none", "ninguna", "formal", "concise", "conciso",
                             "email", "correo"])
    ap.add_argument("--inyector", "--injector", dest="inyector", default=cfg.injector,
                    choices=["auto", "xdotool", "wtype", "ydotool", "clipboard"])
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
                         "~/.local/share/parlar/sesiones/AAAA-MM-DD_HHMM.txt "
                         "(apagado por defecto, ver SECURITY.md)")
    ap.add_argument("--guardar-config", "--save-config", dest="guardar_config",
                    action="store_true",
                    help="persiste los flags actuales en ~/.config/parlar/config.json")
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
    if args.guardar_config:
        cfg.save()
        print("[config] guardada")

    from .app import App  # imports pesados diferidos hasta después del parseo
    App(cfg).ejecutar()


if __name__ == "__main__":
    main()
