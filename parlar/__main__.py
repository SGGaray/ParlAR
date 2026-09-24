"""Punto de entrada: python -m parlar [flags]

Los flags están en español; los flags en inglés también se aceptan
como alias, por si preferís esa nomenclatura.
"""

import argparse
from dataclasses import asdict
import json
import signal
import sys
import threading

from .config import CONFIG_FILE, Config, ErrorConfiguracion
from .control import GuardiaInstancia, InstanciaActivaError
from .runtime_nvidia import preparar_runtime_nvidia

_ALIAS_MODO = {"frase": "utterance"}
_ALIAS_REESCRITURA = {"ninguna": "none", "conciso": "concise", "correo": "email"}
_CODIGO_INSTANCIA_ACTIVA = 1  # fallo operativo; 2 queda para uso/configuración


def _ejecutar_con_sigterm(app):
    """Instala SIGTERM solo durante la ejecución del entry point."""
    solicitud = threading.Event()
    ejecucion_activa = threading.Event()
    ejecucion_activa.set()

    def solicitar_shutdown(_numero, _frame):
        solicitud.set()

    def vigilar_shutdown():
        solicitud.wait()
        if ejecucion_activa.is_set():
            app.salir(esperar=False)

    watcher = threading.Thread(
        target=vigilar_shutdown,
        name="sigterm-watcher",
        daemon=False,
    )
    handler_anterior = signal.signal(signal.SIGTERM, solicitar_shutdown)
    try:
        watcher.start()
        return app.ejecutar()
    finally:
        ejecucion_activa.clear()
        solicitud.set()
        if watcher.is_alive():
            watcher.join()
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
    contexto = ap.add_mutually_exclusive_group()
    contexto.add_argument(
        "--termino-contexto", "--context-term",
        dest="terminos_contexto",
        action="append",
        default=None,
        metavar="TEXTO",
        help="vocabulario o nombre relevante; repetible. "
             "Si se usa, reemplaza la lista configurada",
    )
    contexto.add_argument(
        "--sin-contexto", "--no-context",
        dest="sin_contexto",
        action="store_true",
        help="desactiva el vocabulario/contexto personalizado",
    )
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
    inspeccion = ap.add_mutually_exclusive_group()
    inspeccion.add_argument(
        "--mostrar-config", "--show-config", dest="mostrar_config",
        action="store_true",
        help="muestra la configuración efectiva como JSON y termina",
    )
    inspeccion.add_argument(
        "--ruta-config", "--config-path", dest="ruta_config",
        action="store_true",
        help="muestra la ruta del archivo de configuración y termina",
    )
    inspeccion.add_argument(
        "--mostrar-atajo", "--show-hotkey", dest="mostrar_atajo",
        action="store_true",
        help="muestra el atajo X11 efectivo y termina",
    )
    ap.add_argument(
        "--atajo", "--hotkey", dest="atajo", default=cfg.hotkey_toggle,
        metavar="COMBINACIÓN",
        help="atajo X11; usalo con --guardar-config para persistirlo",
    )
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
    if args.sin_contexto:
        cfg.context_terms = []
    elif args.terminos_contexto is not None:
        cfg.context_terms = args.terminos_contexto
    cfg.mode = _ALIAS_MODO.get(args.modo, args.modo)
    cfg.rewrite_mode = _ALIAS_REESCRITURA.get(args.reescritura, args.reescritura)
    cfg.injector = args.inyector
    if args.sin_indicador:
        cfg.overlay = False
    cfg.guionar = args.guionar
    cfg.guionar_socket = args.guionar_socket
    cfg.guardar_sesion = args.guardar_sesion
    cfg.hotkey_toggle = args.atajo
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

    if args.mostrar_config:
        print(json.dumps(asdict(cfg), indent=2, ensure_ascii=False))
        return
    if args.ruta_config:
        print(CONFIG_FILE)
        return
    if args.mostrar_atajo:
        print(cfg.hotkey_toggle)
        return

    guardia = None
    try:
        guardia = GuardiaInstancia.adquirir_para_entry_point()
        with guardia.preservar_en_reexec():
            preparar_runtime_nvidia(cfg.device)

        from .app import App  # import pesado posterior al lock de instancia
        _ejecutar_con_sigterm(App(cfg, guardia_instancia=guardia))
    except InstanciaActivaError:
        print("ParlAR ya está ejecutándose.", file=sys.stderr)
        raise SystemExit(_CODIGO_INSTANCIA_ACTIVA)
    finally:
        if guardia is not None:
            guardia.liberar()


if __name__ == "__main__":
    main()
