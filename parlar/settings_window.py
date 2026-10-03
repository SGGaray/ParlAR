"""Ventana Tkinter básica para editar Settings persistidos de ParlAR.

La lógica de formulario y persistencia permanece separada de Tk para que el
contrato pueda probarse sin display. Guardar nunca modifica una ``App`` activa:
los cambios se escriben para el próximo inicio mediante ``settings_backend``.
"""

import dataclasses
import queue
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass

from .audio_test import PruebaMicrofono
from .hotkey import (
    ATAJO_PREDETERMINADO,
    ErrorAtajo,
    SesionCapturaAtajo,
    etiqueta_atajo,
    token_desde_evento_tk,
    validar_atajo_principal,
)
from .settings_backend import (
    DispositivoEntrada,
    EstadoAutostart,
    EstadoParlARSettings,
    ErrorDispositivosAudio,
    ErrorSuspensionAtajo,
    ResolucionEntrada,
    ResultadoAutostart,
    ResultadoPersistencia,
    SettingsCapabilities,
    SettingsSnapshot,
    SuspensionHotkeyProductivo,
    cargar_configuracion_recuperable,
    construir_configuracion_candidata,
    consultar_estado_parlar,
    consultar_autostart,
    establecer_autostart,
    listar_dispositivos_entrada,
    obtener_capacidades,
    persistir_configuracion,
    requiere_reinicio,
    resolver_dispositivo_entrada,
    snapshot_configuracion,
)


@dataclass(frozen=True, slots=True)
class ValoresFormulario:
    model_size: str
    device: str
    compute_type: str
    language: str
    context_terms: str
    mode: str
    rewrite_mode: str
    injector: str
    hotkey_toggle: str
    overlay: bool
    guionar: bool
    guionar_socket: str
    guardar_sesion: bool
    audio_input_device: str = "default"
    overlay_position: str = "bottom-center"


@dataclass(frozen=True, slots=True)
class OpcionEntrada:
    valor: str
    etiqueta: str
    disponible: bool


@dataclass(frozen=True, slots=True)
class OpcionSelector:
    """Valor persistible y texto visible de un selector cerrado."""

    valor: str
    etiqueta: str


@dataclass(frozen=True, slots=True)
class ResultadoInventario:
    dispositivos: tuple[DispositivoEntrada, ...]
    error: str | None


@dataclass(frozen=True, slots=True)
class EstadoControlPrueba:
    fase: str
    nivel: float
    error: str | None
    indice: int | None
    seleccion: str | None


@dataclass(frozen=True, slots=True)
class PestañaSettings:
    """Estructura y orden de foco de una pestaña, independiente de Tk."""

    nombre: str
    campos: tuple[str, ...]
    orden_foco: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ArquitecturaSettings:
    pestañas: tuple[PestañaSettings, ...]
    footer_fijo: bool
    footer_dentro_scroll: bool


@dataclass(frozen=True, slots=True)
class RefrescoEntradas:
    inventario: tuple[DispositivoEntrada, ...]
    opciones: tuple[OpcionEntrada, ...]
    indice_seleccionado: int
    seleccion: str
    error: str | None


ARQUITECTURA_SETTINGS = ArquitecturaSettings(
    pestañas=(
        PestañaSettings(
            "Dictado",
            (
                "hotkey_toggle", "audio_input_device", "audio_refresh",
                "audio_test", "language", "context_terms",
            ),
            (
                "hotkey_toggle", "audio_input_device", "audio_refresh",
                "audio_test", "language", "context_terms",
            ),
        ),
        PestañaSettings(
            "Aplicación",
            (
                "runtime_status", "overlay", "overlay_position",
                "autostart",
            ),
            ("overlay", "overlay_position", "autostart"),
        ),
        PestañaSettings(
            "Avanzado",
            (
                "model_size", "device", "compute_type", "mode",
                "rewrite_mode", "injector", "guardar_sesion", "guionar",
                "guionar_socket",
            ),
            (
                "model_size", "device", "compute_type", "mode",
                "rewrite_mode", "injector", "guardar_sesion", "guionar",
                "guionar_socket",
            ),
        ),
    ),
    footer_fijo=True,
    footer_dentro_scroll=False,
)


_ETIQUETAS_SELECTORES = {
    "device": {
        "auto": "Automático",
        "cpu": "CPU",
        "cuda": "NVIDIA CUDA",
    },
    "compute_type": {
        "auto": "Automático",
        "int8": "Enteros de 8 bits (int8)",
        "float16": "Coma flotante de 16 bits (float16)",
        "int8_float16": "Int8 con float16",
    },
    "mode": {
        "utterance": "Por frases",
        "streaming": "Incremental",
    },
    "rewrite_mode": {
        "none": "Sin reescritura",
        "formal": "Formal",
        "concise": "Conciso",
        "email": "Correo",
    },
    "injector": {
        "auto": "Automático",
        "xdotool": "X11 (xdotool)",
        "wtype": "Wayland (wtype)",
        "ydotool": "Teclado virtual (ydotool)",
        "clipboard": "Portapapeles",
    },
    "overlay_position": {
        "top-left": "Arriba · Izquierda",
        "top-center": "Arriba · Centro",
        "top-right": "Arriba · Derecha",
        "middle-left": "Centro · Izquierda",
        "middle-right": "Centro · Derecha",
        "bottom-left": "Abajo · Izquierda",
        "bottom-center": "Abajo · Centro",
        "bottom-right": "Abajo · Derecha",
    },
}


def construir_opciones_selector(
        nombre: str, valores: tuple[str, ...]) -> tuple[OpcionSelector, ...]:
    """Humaniza capacidades reales sin convertir el label en identidad."""
    etiquetas = _ETIQUETAS_SELECTORES.get(nombre, {})
    return tuple(
        OpcionSelector(valor, etiquetas.get(valor, valor))
        for valor in valores
    )


def construir_selectores(
        capacidades: SettingsCapabilities,
) -> dict[str, tuple[OpcionSelector, ...]]:
    return {
        "device": construir_opciones_selector(
            "device", capacidades.devices),
        "compute_type": construir_opciones_selector(
            "compute_type", capacidades.compute_types),
        "mode": construir_opciones_selector(
            "mode", capacidades.modes),
        "rewrite_mode": construir_opciones_selector(
            "rewrite_mode", capacidades.rewrite_modes),
        "injector": construir_opciones_selector(
            "injector", capacidades.injectors),
        "overlay_position": construir_opciones_selector(
            "overlay_position", capacidades.overlay_positions),
    }


def indice_opcion_selector(
        opciones: tuple[OpcionSelector, ...], valor: str) -> int:
    return next(
        (indice for indice, opcion in enumerate(opciones)
         if opcion.valor == valor),
        -1,
    )


def valor_opcion_selector(
        opciones: tuple[OpcionSelector, ...],
        indice: int,
        valor_original: str,
) -> str:
    """Traduce por posición; un índice inválido conserva el valor original."""
    if 0 <= indice < len(opciones):
        return opciones[indice].valor
    return valor_original


def mensaje_reinicio_previo() -> str:
    return "Los cambios se aplican al reiniciar ParlAR."


def accion_cierre_settings(
        *, sucio: bool, descartar_confirmado: bool | None = None) -> str:
    """Decide el cierre sin UI ni efectos secundarios."""
    if not sucio:
        return "cerrar"
    if descartar_confirmado is None:
        return "confirmar"
    return "cerrar" if descartar_confirmado else "seguir_editando"


def cargar_inventario_entradas(
        listar: Callable = listar_dispositivos_entrada) -> ResultadoInventario:
    """Enumera sin impedir que Settings abra si PortAudio no está disponible."""
    try:
        return ResultadoInventario(tuple(listar()), None)
    except ErrorDispositivosAudio as exc:
        return ResultadoInventario((), str(exc))


def _etiqueta_dispositivo(dispositivo: DispositivoEntrada) -> str:
    if dispositivo.host_api:
        return f"{dispositivo.nombre} — {dispositivo.host_api}"
    return dispositivo.nombre


def construir_opciones_entrada(
        seleccion: str,
        inventario: tuple[DispositivoEntrada, ...],
) -> tuple[OpcionEntrada, ...]:
    """Mapea identidad persistible a etiquetas; nunca publica índices."""
    opciones = [OpcionEntrada(
        valor="default",
        etiqueta="Predeterminado del sistema",
        disponible=True,
    )]
    agrupados: dict[str, list[DispositivoEntrada]] = {}
    for dispositivo in inventario:
        agrupados.setdefault(dispositivo.identidad, []).append(dispositivo)
    for identidad, dispositivos in agrupados.items():
        etiqueta = _etiqueta_dispositivo(dispositivos[0])
        if len(dispositivos) > 1:
            etiqueta += f" (ambigua: {len(dispositivos)} dispositivos)"
        opciones.append(OpcionEntrada(
            valor=identidad,
            etiqueta=etiqueta,
            disponible=len(dispositivos) == 1,
        ))
    if seleccion != "default" and seleccion not in agrupados:
        opciones.insert(1, OpcionEntrada(
            valor=seleccion,
            etiqueta="Micrófono guardado no disponible",
            disponible=False,
        ))
    return tuple(opciones)


def refrescar_entradas(
        seleccion: str,
        listar: Callable = listar_dispositivos_entrada,
) -> RefrescoEntradas:
    """Reenumera y conserva la identidad elegida, incluso si desapareció."""
    resultado = cargar_inventario_entradas(listar)
    opciones = construir_opciones_entrada(
        seleccion, resultado.dispositivos)
    return RefrescoEntradas(
        inventario=resultado.dispositivos,
        opciones=opciones,
        indice_seleccionado=_indice_opcion_audio(opciones, seleccion),
        seleccion=seleccion,
        error=resultado.error,
    )


def _indice_opcion_audio(
        opciones: tuple[OpcionEntrada, ...], valor: str) -> int:
    return next(
        (indice for indice, opcion in enumerate(opciones)
         if opcion.valor == valor),
        -1,
    )


def mensaje_resolucion_entrada(
        resolucion: ResolucionEntrada,
        *,
        error_inventario: str | None = None,
) -> str:
    if error_inventario:
        return f"No se pudo obtener el inventario de micrófonos: {error_inventario}"
    if resolucion.usando_fallback and resolucion.dispositivo is not None:
        if resolucion.motivo == "seleccion_ambigua":
            causa = "La selección coincide con varios dispositivos"
        else:
            causa = "El micrófono guardado no está disponible"
        return (
            f"{causa}. La prueba usará temporalmente "
            f"{_etiqueta_dispositivo(resolucion.dispositivo)}."
        )
    mensajes = {
        "inventario_vacio": "No se detectaron dispositivos de entrada.",
        "default_no_disponible": (
            "No hay un dispositivo de entrada predeterminado disponible."),
        "default_ambiguo": (
            "El dispositivo predeterminado es ambiguo y no puede probarse."),
        "seleccion_ausente_inventario_vacio": (
            "El micrófono guardado no está disponible y no se detectaron entradas."),
        "seleccion_ausente_default_no_disponible": (
            "El micrófono guardado no está disponible y no hay un default válido."),
        "seleccion_ambigua_default_no_disponible": (
            "La selección es ambigua y no hay un default válido."),
    }
    return mensajes.get(resolucion.motivo, "")


def guardado_habilitado(
        *, sucio: bool, fase_audio: str, cerrando: bool = False) -> bool:
    """Permite guardar con prueba activa, no durante transiciones o cierre."""
    return (
        sucio
        and not cerrando
        and fase_audio not in {"starting", "stopping", "closing", "closed"}
    )


class ControlPruebaMicrofono:
    """Serializa PortAudio en un worker y sólo publica estado thread-safe."""

    def __init__(
            self,
            inventario: tuple[DispositivoEntrada, ...],
            *,
            prueba=None,
            resolver: Callable = resolver_dispositivo_entrada):
        self.inventario = tuple(inventario)
        self._prueba = prueba or PruebaMicrofono()
        self._resolver = resolver
        self._cv = threading.Condition()
        self._comandos = queue.Queue()
        self._fase = "idle"
        self._nivel = 0.0
        self._error: str | None = None
        self._indice: int | None = None
        self._seleccion: str | None = None
        self._cerrada = threading.Event()
        self._worker = threading.Thread(
            target=self._ejecutar,
            name="settings-audio-test",
            daemon=True,
        )
        self._worker.start()

    def iniciar(self, seleccion: str) -> bool:
        with self._cv:
            if self._fase != "idle":
                return False
            resolucion = self._resolver(seleccion, self.inventario)
            if resolucion.indice is None:
                self._error = (
                    "No hay un dispositivo de entrada resoluble para la prueba "
                    f"({resolucion.motivo or 'sin detalle'})."
                )
                self._cv.notify_all()
                return False
            self._fase = "starting"
            self._nivel = 0.0
            self._error = None
            self._indice = resolucion.indice
            self._seleccion = seleccion
            self._comandos.put(("start", resolucion.indice))
            self._cv.notify_all()
            return True

    def actualizar_inventario(
            self, inventario: tuple[DispositivoEntrada, ...]) -> bool:
        """Reemplaza el inventario sólo cuando no hay un stream en uso."""
        with self._cv:
            if self._fase != "idle":
                return False
            self.inventario = tuple(inventario)
            self._error = None
            self._cv.notify_all()
            return True

    def detener(self) -> bool:
        with self._cv:
            if self._fase not in {"starting", "active"}:
                return False
            self._fase = "stopping"
            self._comandos.put(("stop", None))
            self._cv.notify_all()
            return True

    def cerrar(self) -> bool:
        with self._cv:
            if self._fase in {"closing", "closed"}:
                return False
            self._fase = "closing"
            self._comandos.put(("close", None))
            self._cv.notify_all()
            return True

    def estado(self) -> EstadoControlPrueba:
        try:
            estado_prueba = self._prueba.estado()
        except Exception as exc:
            estado_prueba = None
            error_estado = f"No se pudo consultar la prueba: {exc}"
        else:
            error_estado = None
        with self._cv:
            if self._fase == "active" and estado_prueba is not None:
                self._nivel = estado_prueba.nivel
                if not estado_prueba.activo:
                    self._fase = "idle"
                    self._error = estado_prueba.error
                    self._indice = None
                    self._seleccion = None
                    self._cv.notify_all()
            if error_estado is not None:
                self._error = error_estado
            return EstadoControlPrueba(
                fase=self._fase,
                nivel=self._nivel,
                error=self._error,
                indice=self._indice,
                seleccion=self._seleccion,
            )

    def esperar_fase(self, fase: str, timeout: float = 2.0) -> bool:
        with self._cv:
            return self._cv.wait_for(lambda: self._fase == fase, timeout)

    def esperar_cierre(self, timeout: float = 2.0) -> bool:
        return self._cerrada.wait(timeout)

    def _ejecutar(self):
        while True:
            operacion, indice = self._comandos.get()
            if operacion == "start":
                self._iniciar_worker(indice)
                continue
            if operacion == "stop":
                self._detener_worker(cerrando=False)
                continue
            self._detener_worker(cerrando=True)
            return

    def _iniciar_worker(self, indice: int):
        try:
            iniciada = self._prueba.iniciar(indice)
            if iniciada is False:
                raise RuntimeError("la prueba de micrófono ya estaba activa")
        except Exception as exc:
            with self._cv:
                if self._fase != "closing":
                    self._fase = "idle"
                self._error = f"No se pudo iniciar la prueba: {exc}"
                self._indice = None
                self._seleccion = None
                self._cv.notify_all()
            return
        with self._cv:
            if self._fase == "starting":
                self._fase = "active"
            self._cv.notify_all()

    def _detener_worker(self, *, cerrando: bool):
        error = None
        try:
            self._prueba.detener()
        except Exception as exc:
            error = f"No se pudo detener la prueba: {exc}"
        with self._cv:
            self._nivel = 0.0
            self._indice = None
            self._seleccion = None
            if error is not None:
                self._error = error
            if cerrando:
                self._fase = "closed"
            elif self._fase != "closing":
                self._fase = "idle"
            self._cv.notify_all()
        if cerrando:
            self._cerrada.set()


def parsear_context_terms(texto: str) -> tuple[str, ...]:
    """Interpreta un término por línea; ignora únicamente líneas vacías."""
    return tuple(
        linea.strip()
        for linea in texto.splitlines()
        if linea.strip()
    )


def valores_desde_snapshot(snapshot: SettingsSnapshot) -> ValoresFormulario:
    return ValoresFormulario(
        model_size=snapshot.model_size,
        device=snapshot.device,
        compute_type=snapshot.compute_type,
        language=snapshot.language,
        context_terms="\n".join(snapshot.context_terms),
        mode=snapshot.mode,
        rewrite_mode=snapshot.rewrite_mode,
        injector=snapshot.injector,
        hotkey_toggle=snapshot.hotkey_toggle,
        overlay=snapshot.overlay,
        guionar=snapshot.guionar,
        guionar_socket=snapshot.guionar_socket,
        guardar_sesion=snapshot.guardar_sesion,
        audio_input_device=snapshot.audio_input_device,
        overlay_position=snapshot.overlay_position,
    )


def snapshot_desde_valores(
        inicial: SettingsSnapshot,
        valores: ValoresFormulario) -> SettingsSnapshot:
    datos = dataclasses.asdict(valores)
    datos["context_terms"] = parsear_context_terms(valores.context_terms)
    return dataclasses.replace(inicial, **datos)


def mensaje_persistencia(resultado: ResultadoPersistencia) -> str:
    if resultado.requires_restart:
        return (
            "Configuración guardada. "
            "Los cambios se aplicarán al reiniciar ParlAR."
        )
    return "Configuración guardada. No es necesario reiniciar ParlAR."


class ControlSettings:
    """Coordina formulario y backend sin depender de Tk."""

    def __init__(
            self,
            configuracion_base,
            snapshot_inicial: SettingsSnapshot,
            *,
            persistir: Callable | None = None,
            requiere_reparacion: bool = False):
        self.configuracion_base = configuracion_base
        self.snapshot_inicial = snapshot_inicial
        self._persistir = persistir or persistir_configuracion
        self.requiere_reparacion = requiere_reparacion

    def snapshot_candidato(
            self, valores: ValoresFormulario) -> SettingsSnapshot:
        return snapshot_desde_valores(self.snapshot_inicial, valores)

    def esta_sucio(self, valores: ValoresFormulario) -> bool:
        return self.requiere_reparacion or requiere_reinicio(
            self.snapshot_inicial,
            self.snapshot_candidato(valores),
        )

    def guardar(self, valores: ValoresFormulario) -> ResultadoPersistencia:
        reparacion = self.requiere_reparacion
        candidata = self.snapshot_candidato(valores)
        construir_configuracion_candidata(
            self.configuracion_base,
            candidata,
        )
        resultado = self._persistir(self.configuracion_base, candidata)
        if reparacion and not resultado.requires_restart:
            resultado = dataclasses.replace(
                resultado, requires_restart=True)
        self.snapshot_inicial = resultado.snapshot
        self.requiere_reparacion = False
        return resultado

    def validar_atajo(self, texto: str):
        return validar_atajo_principal(
            texto,
            hotkey_salida=self.configuracion_base.hotkey_quit,
        )

    def cancelar(self, cerrar: Callable[[], None]) -> None:
        cerrar()


class ControlAutostart:
    """Coordina el estado externo de login sin mezclarlo con ``Config``."""

    def __init__(
            self,
            *,
            consultar: Callable[[], EstadoAutostart] = consultar_autostart,
            establecer: Callable[[bool], ResultadoAutostart] = (
                establecer_autostart)):
        self._consultar = consultar
        self._establecer = establecer
        self.estado = consultar()

    def refrescar(self) -> EstadoAutostart:
        self.estado = self._consultar()
        return self.estado

    def cambiar(self, activar: bool):
        resultado = self._establecer(activar)
        self.estado = resultado.estado
        return resultado


class VentanaSettings:
    """Vista nativa; no conoce ``Config`` ni recursos del runtime."""

    def __init__(
            self,
            root,
            control: ControlSettings,
            capacidades: SettingsCapabilities,
            *,
            inventario: tuple[DispositivoEntrada, ...] = (),
            error_inventario: str | None = None,
            control_prueba: ControlPruebaMicrofono | None = None,
            listar_entradas: Callable = listar_dispositivos_entrada,
            estado_parlar=None,
            consultar_estado: Callable | None = None,
            guardia_settings=None,
            permitir_foco_inicial: bool = True,
            puede_presentar: Callable[[], bool] | None = None,
            advertencia_config: str | None = None,
            crear_suspension_atajo: Callable = SuspensionHotkeyProductivo,
            control_autostart: ControlAutostart | None = None):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.control = control
        self.capacidades = capacidades
        self.inventario = tuple(inventario)
        self.error_inventario = error_inventario
        self._listar_entradas = listar_entradas
        self._crear_suspension_atajo = crear_suspension_atajo
        self._consultar_estado = consultar_estado or consultar_estado_parlar
        self._guardia_settings = guardia_settings
        self._puede_presentar = puede_presentar or (lambda: True)
        self._presentacion_pendiente = not permitir_foco_inicial
        self._presentacion_visible = permitir_foco_inicial
        self._poll_runtime_id = None
        self._advertencia_config = advertencia_config
        self.control_autostart = control_autostart or ControlAutostart()
        self.control_prueba = control_prueba or ControlPruebaMicrofono(
            self.inventario)
        self._creando = True
        self._cerrando = False
        self._poll_audio_id = None
        self._refresh_audio_thread = None
        self._refresh_audio_resultados = queue.Queue()
        self._modal_descarte = None
        self._modal_atajo = None
        self._captura_atajo = None
        self._suspension_atajo = None
        self._renovacion_atajo_id = None
        self._boton_origen_atajo = None
        self._areas_scroll = {}
        self._widgets_foco = {}
        self.estado_parlar = estado_parlar or consultar_estado_parlar()

        if not permitir_foco_inicial:
            self.root.withdraw()

        self.opciones_audio = construir_opciones_entrada(
            control.snapshot_inicial.audio_input_device,
            self.inventario,
        )
        self._indice_audio_inicial = _indice_opcion_audio(
            self.opciones_audio,
            control.snapshot_inicial.audio_input_device,
        )
        self.opciones_selectores = construir_selectores(capacidades)
        self.selectores = {}

        self.root.title("Configuración de ParlAR")
        self.root.geometry("700x700")
        self.root.minsize(640, 600)
        self.root.protocol("WM_DELETE_WINDOW", self._solicitar_cierre)
        self.root.bind("<Escape>", self._al_escape)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        self._configurar_estilos()
        self._crear_variables(
            valores_desde_snapshot(control.snapshot_inicial))
        self._crear_contenido()
        self._conectar_cambios()
        self._creando = False
        self._actualizar_dependencias()
        self._actualizar_audio()
        self._actualizar_sucio()
        self._programar_poll_audio()
        self._programar_poll_runtime()
        if permitir_foco_inicial:
            self.root.after_idle(self._presentar)

    def _configurar_estilos(self):
        estilo = self.ttk.Style(self.root)
        estilo.configure("Status.TLabel", foreground="#4b5563")
        estilo.configure("Success.Status.TLabel", foreground="#166534")
        estilo.configure("Warning.Status.TLabel", foreground="#92400e")
        estilo.configure("Error.Status.TLabel", foreground="#b91c1c")
        estilo.configure(
            "Section.TLabel", font=("TkDefaultFont", 11, "bold"))

    def _crear_variables(self, valores: ValoresFormulario):
        tk = self.tk
        self.variables = {
            "model_size": tk.StringVar(value=valores.model_size),
            "device": tk.StringVar(value=valores.device),
            "compute_type": tk.StringVar(value=valores.compute_type),
            "language": tk.StringVar(value=valores.language),
            "mode": tk.StringVar(value=valores.mode),
            "rewrite_mode": tk.StringVar(value=valores.rewrite_mode),
            "injector": tk.StringVar(value=valores.injector),
            "hotkey_toggle": tk.StringVar(value=valores.hotkey_toggle),
            "overlay": tk.BooleanVar(value=valores.overlay),
            "guionar": tk.BooleanVar(value=valores.guionar),
            "guionar_socket": tk.StringVar(value=valores.guionar_socket),
            "guardar_sesion": tk.BooleanVar(value=valores.guardar_sesion),
            "overlay_position": tk.StringVar(
                value=valores.overlay_position),
        }
        self.estado = tk.StringVar(
            value=self._advertencia_config or mensaje_reinicio_previo())
        self.estado_runtime_titulo = tk.StringVar(
            value=self.estado_parlar.titulo)
        self.estado_runtime_mensaje = tk.StringVar(
            value=self.estado_parlar.mensaje)
        self.audio_seleccion = tk.StringVar(value="")
        self.estado_audio = tk.StringVar(value="")
        self.hotkey_etiqueta = tk.StringVar(
            value=etiqueta_atajo(valores.hotkey_toggle))
        self.autostart_activado = tk.BooleanVar(
            value=self.control_autostart.estado.activado)
        self.estado_autostart = tk.StringVar(
            value=self.control_autostart.estado.mensaje)
        self._contexto_inicial = valores.context_terms

    def _crear_contenido(self):
        ttk = self.ttk
        self.notebook = ttk.Notebook(self.root)
        self.notebook.grid(
            row=0, column=0, sticky="nsew", padx=16, pady=(16, 0))
        self.notebook.enable_traversal()

        dictado = self._crear_pestana_scroll("Dictado")
        aplicacion = self._crear_pestana_scroll("Aplicación")
        avanzado = self._crear_pestana_scroll("Avanzado")
        self._crear_dictado(dictado)
        self._crear_aplicacion(aplicacion)
        self._crear_avanzado(avanzado)

        self.footer = ttk.Frame(self.root, padding=(16, 10, 16, 12))
        self.footer.grid(row=1, column=0, sticky="ew")
        self.footer.columnconfigure(0, weight=1)
        self.etiqueta_estado = ttk.Label(
            self.footer,
            textvariable=self.estado,
            style="Status.TLabel",
            wraplength=420,
        )
        self.etiqueta_estado.grid(row=0, column=0, sticky="w")
        if self._advertencia_config:
            self.etiqueta_estado.configure(style="Error.Status.TLabel")
        self.boton_cancelar = ttk.Button(
            self.footer,
            text="Cerrar",
            command=self._solicitar_cierre,
        )
        self.boton_cancelar.grid(row=0, column=1, padx=(16, 8))
        self.boton_guardar = ttk.Button(
            self.footer, text="Guardar cambios", command=self._guardar)
        self.boton_guardar.grid(row=0, column=2)

        self.root.bind("<MouseWheel>", self._rueda_scroll, add="+")
        self.root.bind("<Button-4>", self._rueda_scroll, add="+")
        self.root.bind("<Button-5>", self._rueda_scroll, add="+")
        self.root.bind("<Prior>", self._pagina_scroll, add="+")
        self.root.bind("<Next>", self._pagina_scroll, add="+")

    def _crear_pestana_scroll(self, nombre):
        ttk = self.ttk
        pestana = ttk.Frame(self.notebook)
        pestana.columnconfigure(0, weight=1)
        pestana.rowconfigure(0, weight=1)
        self.notebook.add(pestana, text=nombre)

        canvas = self.tk.Canvas(
            pestana, borderwidth=0, highlightthickness=0, takefocus=0)
        barra = ttk.Scrollbar(
            pestana, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=barra.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        barra.grid(row=0, column=1, sticky="ns")

        contenido = ttk.Frame(canvas, padding=(24, 20, 24, 24))
        contenido.columnconfigure(1, weight=1)
        ventana = canvas.create_window(
            (0, 0), window=contenido, anchor="nw")
        area = {
            "canvas": canvas,
            "contenido": contenido,
            "ventana": ventana,
            "pestana": pestana,
        }
        self._areas_scroll[nombre] = area
        contenido.bind(
            "<Configure>",
            lambda _evento, lienzo=canvas: self._actualizar_region_scroll(
                lienzo),
        )
        canvas.bind(
            "<Configure>",
            lambda evento, lienzo=canvas, item=ventana:
                self._ajustar_ancho_contenido(evento, lienzo, item),
        )
        return contenido

    def _titulo_seccion(self, padre, texto, fila):
        self.ttk.Label(
            padre, text=texto, style="Section.TLabel").grid(
                row=fila,
                column=0,
                columnspan=3,
                sticky="w",
                pady=((0 if fila == 0 else 20), 8),
            )

    def _separador(self, padre, fila):
        self.ttk.Separator(padre).grid(
            row=fila, column=0, columnspan=3, sticky="ew", pady=(20, 0))

    def _registrar_foco(self, nombre, widget):
        self._widgets_foco[nombre] = widget
        widget.bind(
            "<FocusIn>",
            lambda _evento, control=widget:
                self.root.after_idle(
                    lambda: self._asegurar_foco_visible(control)),
            add="+",
        )

    def _crear_dictado(self, contenido):
        ttk = self.ttk
        self._titulo_seccion(contenido, "Atajo", 0)
        ttk.Label(contenido, text="Atajo de dictado").grid(
            row=1, column=0, padx=(0, 16), sticky="w")
        fila_atajo = ttk.Frame(contenido)
        fila_atajo.grid(row=1, column=1, columnspan=2, sticky="ew")
        fila_atajo.columnconfigure(0, weight=1)
        self.etiqueta_hotkey = ttk.Label(
            fila_atajo,
            textvariable=self.hotkey_etiqueta,
            anchor="w",
            padding=(10, 6),
            relief="sunken",
        )
        self.etiqueta_hotkey.grid(row=0, column=0, sticky="ew")
        self.boton_cambiar_atajo = ttk.Button(
            fila_atajo,
            text="Cambiar…",
            command=self._mostrar_captura_atajo,
        )
        self.boton_cambiar_atajo.grid(row=0, column=1, padx=(10, 0))
        self.boton_cambiar_atajo.bind(
            "<Return>",
            lambda _evento: self._mostrar_captura_atajo() or "break",
        )
        self._registrar_foco("hotkey_toggle", self.boton_cambiar_atajo)
        ttk.Label(
            contenido,
            text="Mantené el atajo para hablar. Soltalo para terminar.",
            style="Status.TLabel",
        ).grid(row=2, column=1, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(
            contenido,
            text=(
                "Doble toque activa el dictado continuo. "
                "Escape cancela lo pendiente."
            ),
            style="Status.TLabel",
            wraplength=540,
        ).grid(row=3, column=1, sticky="w", pady=(2, 0))
        self.boton_restaurar_atajo = ttk.Button(
            contenido,
            text="Restaurar predeterminado",
            command=self._restaurar_atajo,
        )
        self.boton_restaurar_atajo.grid(
            row=3, column=2, sticky="e", padx=(10, 0))

        self._separador(contenido, 4)
        self._titulo_seccion(contenido, "Micrófono", 5)
        ttk.Label(contenido, text="Entrada de audio").grid(
            row=6, column=0, padx=(0, 16), sticky="w")
        self.selector_audio = ttk.Combobox(
            contenido,
            textvariable=self.audio_seleccion,
            values=tuple(opcion.etiqueta for opcion in self.opciones_audio),
            state="readonly",
        )
        self.selector_audio.grid(row=6, column=1, sticky="ew")
        self._registrar_foco("audio_input_device", self.selector_audio)
        if self._indice_audio_inicial >= 0:
            self.selector_audio.current(self._indice_audio_inicial)
        self.boton_actualizar_audio = ttk.Button(
            contenido,
            text="Actualizar",
            command=self._actualizar_dispositivos,
        )
        self.boton_actualizar_audio.grid(row=6, column=2, padx=(10, 0))
        self._registrar_foco("audio_refresh", self.boton_actualizar_audio)
        self.boton_prueba = ttk.Button(
            contenido,
            text="Probar micrófono",
            command=self._alternar_prueba_audio,
        )
        self.boton_prueba.grid(row=7, column=1, sticky="w", pady=(12, 0))
        self._registrar_foco("audio_test", self.boton_prueba)
        self.medidor_audio = ttk.Progressbar(
            contenido, maximum=100, mode="determinate", length=180)
        self.medidor_audio.grid(
            row=7, column=2, sticky="ew", padx=(10, 0), pady=(12, 0))
        self.etiqueta_estado_audio = ttk.Label(
            contenido,
            textvariable=self.estado_audio,
            style="Status.TLabel",
            wraplength=560,
        )
        self.etiqueta_estado_audio.grid(
            row=8, column=1, columnspan=2, sticky="w", pady=(6, 0))

        self._separador(contenido, 9)
        self._titulo_seccion(contenido, "Idioma", 10)
        ttk.Label(contenido, text="Idioma del dictado").grid(
            row=11, column=0, padx=(0, 16), sticky="w")
        idioma = ttk.Entry(
            contenido, textvariable=self.variables["language"])
        idioma.grid(row=11, column=1, columnspan=2, sticky="ew")
        self._registrar_foco("language", idioma)
        ttk.Label(
            contenido,
            text="Usá un código como es o en. Vacío detecta el idioma.",
            style="Status.TLabel",
        ).grid(row=12, column=1, columnspan=2, sticky="w", pady=(6, 0))

        self._separador(contenido, 13)
        self._titulo_seccion(contenido, "Palabras y nombres", 14)
        ttk.Label(
            contenido,
            text=(
                "Agregá nombres propios, siglas o términos que ParlAR "
                "debería reconocer mejor. Un término por línea."
            ),
            style="Status.TLabel",
            wraplength=560,
        ).grid(row=15, column=0, columnspan=3, sticky="w")
        self.contexto = self.tk.Text(
            contenido,
            height=6,
            wrap="word",
            undo=True,
            relief="solid",
            borderwidth=1,
        )
        self.contexto.grid(
            row=16, column=0, columnspan=3, sticky="nsew", pady=(8, 0))
        self.contexto.insert("1.0", self._contexto_inicial)
        self.contexto.edit_modified(False)
        self._registrar_foco("context_terms", self.contexto)

    def _actualizar_valor_atajo(self, valor: str) -> None:
        self.variables["hotkey_toggle"].set(valor)
        self.hotkey_etiqueta.set(etiqueta_atajo(valor))

    def _restaurar_atajo(self) -> None:
        self._actualizar_valor_atajo(ATAJO_PREDETERMINADO)

    def _mostrar_captura_atajo(self) -> None:
        if self._modal_atajo is not None or self._cerrando:
            return
        suspension = self._crear_suspension_atajo()
        self._suspension_atajo = suspension
        self._captura_atajo = SesionCapturaAtajo(
            self.variables["hotkey_toggle"].get(),
            validar=self.control.validar_atajo,
            suspension=suspension,
        )
        self._boton_origen_atajo = self.boton_cambiar_atajo
        modal = self.tk.Toplevel(self.root)
        self._modal_atajo = modal
        modal.title("Cambiar atajo")
        modal.resizable(False, False)
        modal.transient(self.root)
        modal.protocol("WM_DELETE_WINDOW", self._cancelar_captura_atajo)

        cuerpo = self.ttk.Frame(modal, padding=20)
        cuerpo.grid(row=0, column=0, sticky="nsew")
        cuerpo.columnconfigure(0, weight=1)
        self.ttk.Label(
            cuerpo,
            text="Presioná la combinación que querés usar",
            style="Section.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        self.ttk.Label(
            cuerpo,
            text=(
                "Podés usar modificadores izquierdos o derechos. "
                "Escape cancela y Tab recorre los controles."
            ),
            style="Status.TLabel",
            wraplength=430,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 14))

        self._captura_atajo_texto = self.tk.StringVar(
            value="Esperando combinación…")
        self._captura_atajo_error = self.tk.StringVar(value="")
        self._superficie_captura_atajo = self.tk.Frame(
            cuerpo,
            padx=14,
            pady=12,
            relief="sunken",
            borderwidth=1,
            takefocus=1,
            highlightthickness=2,
            highlightcolor="#2563eb",
            highlightbackground="#9ca3af",
        )
        self._superficie_captura_atajo.grid(
            row=2, column=0, columnspan=2, sticky="ew")
        self.ttk.Label(
            self._superficie_captura_atajo,
            textvariable=self._captura_atajo_texto,
            anchor="center",
        ).grid(row=0, column=0, sticky="ew")
        self._superficie_captura_atajo.columnconfigure(0, weight=1)
        self.ttk.Label(
            cuerpo,
            textvariable=self._captura_atajo_error,
            style="Error.Status.TLabel",
            wraplength=430,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 14))

        cancelar = self.ttk.Button(
            cuerpo, text="Cancelar", command=self._cancelar_captura_atajo)
        cancelar.grid(row=4, column=0, sticky="e", padx=(0, 8))
        cancelar.bind(
            "<Return>",
            lambda _evento: self._cancelar_captura_atajo() or "break",
        )
        self._boton_usar_atajo = self.ttk.Button(
            cuerpo, text="Usar combinación", command=self._usar_captura_atajo)
        self._boton_usar_atajo.grid(row=4, column=1, sticky="e")
        self._boton_usar_atajo.bind(
            "<Return>",
            lambda _evento: self._usar_captura_atajo() or "break",
        )
        self._boton_usar_atajo.state(["disabled"])

        self._superficie_captura_atajo.bind(
            "<KeyPress>", self._al_press_captura_atajo)
        self._superficie_captura_atajo.bind(
            "<KeyRelease>", self._al_release_captura_atajo)
        modal.bind("<FocusOut>", self._al_perder_foco_captura, add="+")
        try:
            self._captura_atajo.iniciar(
                runtime_activo=self.estado_parlar.ejecutandose)
            modal.grab_set()
            self._superficie_captura_atajo.focus_set()
        except (ErrorSuspensionAtajo, OSError) as exc:
            self.estado.set(str(exc))
            self.etiqueta_estado.configure(style="Error.Status.TLabel")
            self._cerrar_captura_atajo()
            return
        self._programar_renovacion_atajo()

    def _al_press_captura_atajo(self, evento):
        token = token_desde_evento_tk(evento.keysym, evento.char)
        accion = self._captura_atajo.presionar(token)
        if accion == "cancelar":
            self._cancelar_captura_atajo()
            return "break"
        if accion == "ignorar":
            return None
        candidata = self._captura_atajo.captura.candidata
        if candidata is None:
            return "break"
        self._captura_atajo_texto.set(candidata.etiqueta)
        try:
            self.control.validar_atajo(candidata.persistido)
        except ErrorAtajo as exc:
            self._captura_atajo_error.set(str(exc))
            self._boton_usar_atajo.state(["disabled"])
        else:
            self._captura_atajo_error.set("")
            self._boton_usar_atajo.state(["!disabled"])
        return "break"

    def _al_release_captura_atajo(self, evento):
        token = token_desde_evento_tk(evento.keysym, evento.char)
        self._captura_atajo.soltar(token)
        return None if token == "<tab>" else "break"

    def _usar_captura_atajo(self) -> None:
        try:
            normalizada = self._captura_atajo.usar()
        except ErrorAtajo as exc:
            self._captura_atajo_error.set(str(exc))
            self._boton_usar_atajo.state(["disabled"])
            return
        self._actualizar_valor_atajo(normalizada.persistido)
        self._cerrar_captura_atajo()

    def _cancelar_captura_atajo(self, _evento=None) -> None:
        if self._captura_atajo is not None:
            self._captura_atajo.cancelar()
        self._cerrar_captura_atajo()

    def _programar_renovacion_atajo(self) -> None:
        if (
            self._modal_atajo is not None
            and self._suspension_atajo is not None
            and self._suspension_atajo.adquirida
        ):
            self._renovacion_atajo_id = self.root.after(
                2000, self._renovar_suspension_atajo)

    def _renovar_suspension_atajo(self) -> None:
        self._renovacion_atajo_id = None
        captura = self._captura_atajo
        if captura is None or self._modal_atajo is None:
            return
        try:
            renovada = captura.renovar()
        except Exception:
            renovada = False
        if not renovada:
            self.estado.set(
                "La captura se canceló porque no pudo mantenerse suspendido "
                "el atajo activo.")
            self.etiqueta_estado.configure(style="Error.Status.TLabel")
            self._cerrar_captura_atajo()
            return
        self._programar_renovacion_atajo()

    def _al_perder_foco_captura(self, _evento=None) -> None:
        self.root.after_idle(self._cancelar_si_perdio_foco_captura)

    def _cancelar_si_perdio_foco_captura(self) -> None:
        modal = self._modal_atajo
        if modal is None:
            return
        try:
            widget = modal.focus_get()
            dentro = False
            while widget is not None:
                if widget == modal:
                    dentro = True
                    break
                padre = widget.winfo_parent()
                widget = widget.nametowidget(padre) if padre else None
        except Exception:
            dentro = False
        if not dentro:
            self._cancelar_captura_atajo()

    def _cerrar_captura_atajo(self) -> None:
        renovacion, self._renovacion_atajo_id = (
            self._renovacion_atajo_id, None)
        if renovacion is not None:
            try:
                self.root.after_cancel(renovacion)
            except Exception:
                pass
        self._suspension_atajo = None
        captura, self._captura_atajo = self._captura_atajo, None
        if captura is not None:
            try:
                captura.cerrar()
            except Exception:
                pass
        modal, self._modal_atajo = self._modal_atajo, None
        if modal is not None:
            try:
                modal.grab_release()
                modal.destroy()
            except Exception:
                pass
        origen, self._boton_origen_atajo = self._boton_origen_atajo, None
        if origen is not None and not self._cerrando:
            try:
                origen.focus_set()
            except Exception:
                pass

    def _crear_aplicacion(self, contenido):
        ttk = self.ttk
        self._titulo_seccion(contenido, "Estado de ParlAR", 0)
        self.etiqueta_runtime_titulo = ttk.Label(
            contenido,
            textvariable=self.estado_runtime_titulo,
            style="Section.TLabel",
        )
        self.etiqueta_runtime_titulo.grid(
            row=1, column=0, columnspan=3, sticky="w")
        self.etiqueta_runtime_mensaje = ttk.Label(
            contenido,
            textvariable=self.estado_runtime_mensaje,
            wraplength=560,
        )
        self.etiqueta_runtime_mensaje.grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self._aplicar_estado_parlar(self.estado_parlar)

        self._separador(contenido, 3)
        self._titulo_seccion(contenido, "Indicador", 4)
        self.check_overlay = ttk.Checkbutton(
            contenido,
            text="Mostrar indicador durante el dictado",
            variable=self.variables["overlay"],
        )
        self.check_overlay.grid(
            row=5, column=0, columnspan=3, sticky="w")
        self._registrar_foco("overlay", self.check_overlay)
        self._campo(
            contenido,
            "Posición del indicador",
            "overlay_position",
            6,
            0,
            opciones=self.opciones_selectores["overlay_position"],
            ayuda="Elegí dónde aparece la señal visual mientras dictás.",
        )

        self._separador(contenido, 8)
        self._titulo_seccion(contenido, "Inicio de sesión", 9)
        self.check_autostart = ttk.Checkbutton(
            contenido,
            text="Iniciar ParlAR al iniciar sesión",
            variable=self.autostart_activado,
            command=self._alternar_autostart,
        )
        self.check_autostart.grid(
            row=10, column=0, columnspan=3, sticky="w")
        self._registrar_foco("autostart", self.check_autostart)
        self.etiqueta_autostart = ttk.Label(
            contenido,
            textvariable=self.estado_autostart,
            style="Status.TLabel",
            wraplength=560,
        )
        self.etiqueta_autostart.grid(
            row=11, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self._aplicar_estado_autostart(self.control_autostart.estado)
        ttk.Label(
            contenido,
            text=mensaje_reinicio_previo(),
            style="Status.TLabel",
            wraplength=560,
        ).grid(row=12, column=0, columnspan=3, sticky="w", pady=(18, 0))

    def _aplicar_estado_autostart(
            self, estado: EstadoAutostart, *, mensaje: str | None = None):
        self.autostart_activado.set(estado.activado)
        self.estado_autostart.set(mensaje or estado.mensaje)
        if estado.modificable and not self._cerrando:
            self.check_autostart.state(["!disabled"])
        else:
            self.check_autostart.state(["disabled"])
        estilo = (
            "Error.Status.TLabel"
            if estado.estado == "error"
            else "Status.TLabel"
        )
        self.etiqueta_autostart.configure(style=estilo)

    def _alternar_autostart(self):
        deseado = bool(self.autostart_activado.get())
        resultado = self.control_autostart.cambiar(deseado)
        self._aplicar_estado_autostart(
            resultado.estado,
            mensaje=resultado.mensaje,
        )

    def _crear_avanzado(self, contenido):
        ttk = self.ttk
        self._titulo_seccion(contenido, "Reconocimiento", 0)
        self._campo(
            contenido, "Modelo de reconocimiento", "model_size", 1, 0,
            ayuda="Tamaño del modelo de voz, por ejemplo small o medium.")
        self._campo(
            contenido, "Acelerador", "device", 3, 0,
            opciones=self.opciones_selectores["device"],
            ayuda="Automático elige CPU o NVIDIA CUDA al iniciar.")
        self._campo(
            contenido, "Precisión de cálculo", "compute_type", 5, 0,
            opciones=self.opciones_selectores["compute_type"],
            ayuda="Formato numérico usado por el modelo de reconocimiento.")
        self._campo(
            contenido, "Estrategia de transcripción", "mode", 7, 0,
            opciones=self.opciones_selectores["mode"],
            ayuda="Por frases espera una pausa; Incremental actualiza el texto.")

        self._separador(contenido, 9)
        self._titulo_seccion(contenido, "Salida y texto", 10)
        self._campo(
            contenido, "Reescritura", "rewrite_mode", 11, 0,
            opciones=self.opciones_selectores["rewrite_mode"],
            ayuda="Ajusta el estilo del texto después del reconocimiento.")
        self._campo(
            contenido, "Método de escritura", "injector", 13, 0,
            opciones=self.opciones_selectores["injector"],
            ayuda="Cómo se entrega el texto a la aplicación con foco.")
        self.check_guardar_sesion = ttk.Checkbutton(
            contenido,
            text="Conservar transcripciones",
            variable=self.variables["guardar_sesion"],
        )
        self.check_guardar_sesion.grid(
            row=15, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self._registrar_foco("guardar_sesion", self.check_guardar_sesion)

        self._separador(contenido, 16)
        self._titulo_seccion(contenido, "GuionAR", 17)
        self.check_guionar = ttk.Checkbutton(
            contenido,
            text="Enviar dictado y actividad a GuionAR",
            variable=self.variables["guionar"],
        )
        self.check_guionar.grid(
            row=18, column=0, columnspan=3, sticky="w")
        self._registrar_foco("guionar", self.check_guionar)
        ttk.Label(contenido, text="Socket de GuionAR").grid(
            row=19, column=0, padx=(0, 16), pady=(10, 0), sticky="w")
        self.entrada_guionar_socket = ttk.Entry(
            contenido, textvariable=self.variables["guionar_socket"])
        self.entrada_guionar_socket.grid(
            row=19, column=1, columnspan=2, sticky="ew", pady=(10, 0))
        self._registrar_foco(
            "guionar_socket", self.entrada_guionar_socket)
        ttk.Label(
            contenido,
            text="La ruta se conserva aunque desactives GuionAR.",
            style="Status.TLabel",
        ).grid(row=20, column=1, columnspan=2, sticky="w", pady=(6, 0))

    def _actualizar_region_scroll(self, canvas):
        region = canvas.bbox("all")
        if region is not None:
            canvas.configure(scrollregion=region)

    def _ajustar_ancho_contenido(self, evento, canvas, ventana):
        canvas.itemconfigure(ventana, width=evento.width)

    def _area_scroll_de_widget(self, widget):
        actual = widget
        while actual is not None:
            for area in self._areas_scroll.values():
                if actual == area["contenido"]:
                    return area
            try:
                padre = actual.winfo_parent()
                actual = actual.nametowidget(padre) if padre else None
            except Exception:
                return None
        return None

    def _widget_bajo_puntero(self, evento):
        try:
            return self.root.winfo_containing(evento.x_root, evento.y_root)
        except Exception:
            return None

    def _rueda_scroll(self, evento):
        widget = self._widget_bajo_puntero(evento)
        area = self._area_scroll_de_widget(widget)
        if area is None or widget.winfo_class() == "Text":
            return None
        if getattr(evento, "num", None) == 4:
            pasos = -3
        elif getattr(evento, "num", None) == 5:
            pasos = 3
        else:
            delta = getattr(evento, "delta", 0)
            if not delta:
                return None
            pasos = -3 if delta > 0 else 3
        area["canvas"].yview_scroll(pasos, "units")
        return "break"

    def _pagina_scroll(self, evento):
        widget = self.root.focus_get()
        area = self._area_scroll_de_widget(widget)
        if area is None or widget.winfo_class() == "Text":
            return None
        pasos = -1 if evento.keysym == "Prior" else 1
        area["canvas"].yview_scroll(pasos, "pages")
        return "break"

    def _asegurar_foco_visible(self, widget):
        area = self._area_scroll_de_widget(widget)
        if area is None:
            return
        canvas = area["canvas"]
        contenido = area["contenido"]
        canvas.update_idletasks()
        alto_total = max(1, contenido.winfo_reqheight())
        alto_vista = max(1, canvas.winfo_height())
        superior = canvas.canvasy(0)
        inferior = superior + alto_vista
        y = widget.winfo_rooty() - contenido.winfo_rooty()
        alto = max(1, widget.winfo_height())
        if y < superior:
            canvas.yview_moveto(max(0.0, y / alto_total))
        elif y + alto > inferior:
            destino = (y + alto - alto_vista) / alto_total
            canvas.yview_moveto(min(1.0, max(0.0, destino)))

    def _campo(
            self, padre, etiqueta, nombre, fila, columna, *, opciones=None,
            ayuda=None):
        self.ttk.Label(padre, text=etiqueta).grid(
            row=fila, column=columna, padx=(0, 8), pady=5, sticky="w")
        if opciones is None:
            widget = self.ttk.Entry(
                padre, textvariable=self.variables[nombre])
        else:
            widget = self.ttk.Combobox(
                padre,
                textvariable=self.variables[nombre],
                values=tuple(opcion.etiqueta for opcion in opciones),
                state="readonly",
            )
            indice = indice_opcion_selector(
                opciones, self.variables[nombre].get())
            if indice >= 0:
                widget.current(indice)
            self.selectores[nombre] = (widget, opciones)
        widget.grid(
            row=fila, column=columna + 1,
            columnspan=2,
            pady=5,
            sticky="ew")
        self._registrar_foco(nombre, widget)
        if ayuda:
            self.ttk.Label(
                padre,
                text=ayuda,
                style="Status.TLabel",
                wraplength=560,
            ).grid(
                row=fila + 1,
                column=columna + 1,
                columnspan=2,
                pady=(0, 5),
                sticky="nw",
            )

    def _valor_selector_actual(self, nombre: str) -> str:
        selector, opciones = self.selectores[nombre]
        original = getattr(self.control.snapshot_inicial, nombre)
        return valor_opcion_selector(
            opciones, selector.current(), original)

    def _conectar_cambios(self):
        for variable in self.variables.values():
            variable.trace_add("write", self._al_cambiar)
        self.selector_audio.bind(
            "<<ComboboxSelected>>", self._al_cambiar_audio)
        self.contexto.bind("<<Modified>>", self._al_modificar_contexto)

    def _al_cambiar(self, *_args):
        self._actualizar_dependencias()
        self._actualizar_sucio()

    def _al_cambiar_audio(self, *_args):
        self._actualizar_audio()
        self._actualizar_sucio()

    def _al_modificar_contexto(self, _evento):
        if self.contexto.edit_modified():
            self.contexto.edit_modified(False)
            self._actualizar_sucio()

    def _actualizar_dependencias(self):
        if not hasattr(self, "entrada_guionar_socket"):
            return
        self.entrada_guionar_socket.configure(
            state="normal" if self.variables["guionar"].get() else "disabled")
        selector_posicion, _opciones = self.selectores["overlay_position"]
        selector_posicion.configure(
            state=(
                "readonly"
                if self.variables["overlay"].get()
                else "disabled"
            )
        )

    def _valores(self) -> ValoresFormulario:
        return ValoresFormulario(
            model_size=self.variables["model_size"].get(),
            device=self._valor_selector_actual("device"),
            compute_type=self._valor_selector_actual("compute_type"),
            language=self.variables["language"].get(),
            context_terms=self.contexto.get("1.0", "end-1c"),
            mode=self._valor_selector_actual("mode"),
            rewrite_mode=self._valor_selector_actual("rewrite_mode"),
            injector=self._valor_selector_actual("injector"),
            hotkey_toggle=self.variables["hotkey_toggle"].get(),
            overlay=self.variables["overlay"].get(),
            guionar=self.variables["guionar"].get(),
            guionar_socket=self.variables["guionar_socket"].get(),
            guardar_sesion=self.variables["guardar_sesion"].get(),
            audio_input_device=self._audio_actual(),
            overlay_position=self._valor_selector_actual(
                "overlay_position"),
        )

    def _audio_actual(self) -> str:
        indice = self.selector_audio.current()
        if 0 <= indice < len(self.opciones_audio):
            return self.opciones_audio[indice].valor
        return self.control.snapshot_inicial.audio_input_device

    def _resolucion_audio(self) -> ResolucionEntrada:
        return resolver_dispositivo_entrada(
            self._audio_actual(), self.inventario)

    def _actualizar_sucio(self):
        if self._creando:
            return
        fase = self.control_prueba.estado().fase
        sucio = self.control.esta_sucio(self._valores())
        if guardado_habilitado(
                sucio=sucio,
                fase_audio=fase,
                cerrando=self._cerrando):
            self.boton_guardar.state(["!disabled"])
        else:
            self.boton_guardar.state(["disabled"])
        self.boton_cancelar.configure(
            text="Descartar cambios" if sucio else "Cerrar")

    def _refresh_activo(self) -> bool:
        return (
            self._refresh_audio_thread is not None
            and self._refresh_audio_thread.is_alive()
        )

    def _actualizar_audio(self):
        estado = self.control_prueba.estado()
        self.medidor_audio["value"] = estado.nivel * 100
        ocupado = estado.fase in {
            "starting", "active", "stopping", "closing", "closed"
        }
        refrescando = self._refresh_activo()
        self.selector_audio.configure(
            state="disabled" if ocupado or refrescando else "readonly")
        if ocupado or refrescando or self._cerrando:
            self.boton_actualizar_audio.state(["disabled"])
        else:
            self.boton_actualizar_audio.state(["!disabled"])

        if self._cerrando:
            self.boton_prueba.configure(text="Probar micrófono")
            self.boton_prueba.state(["disabled"])
            mensaje = "Cerrando la prueba de audio…"
            estilo = "Status.TLabel"
        elif refrescando:
            self.boton_prueba.configure(text="Probar micrófono")
            self.boton_prueba.state(["disabled"])
            mensaje = "Actualizando la lista de micrófonos…"
            estilo = "Status.TLabel"
        elif estado.fase == "active":
            self.boton_prueba.configure(text="Detener prueba")
            self.boton_prueba.state(["!disabled"])
            mensaje = "Prueba activa. El audio no se guarda."
            estilo = "Status.TLabel"
        elif estado.fase in {"starting", "stopping"}:
            texto_boton = (
                "Iniciando…" if estado.fase == "starting" else "Deteniendo…")
            self.boton_prueba.configure(text=texto_boton)
            self.boton_prueba.state(["disabled"])
            mensaje = (
                "Abriendo el dispositivo de entrada…"
                if estado.fase == "starting"
                else "Cerrando el dispositivo de entrada…"
            )
            estilo = "Status.TLabel"
        elif estado.fase in {"closing", "closed"}:
            self.boton_prueba.configure(text="Probar micrófono")
            self.boton_prueba.state(["disabled"])
            mensaje = "Cerrando la prueba de audio…"
            estilo = "Status.TLabel"
        else:
            resolucion = self._resolucion_audio()
            self.boton_prueba.configure(text="Probar micrófono")
            if resolucion.indice is None or self.error_inventario:
                self.boton_prueba.state(["disabled"])
            else:
                self.boton_prueba.state(["!disabled"])
            mensaje = mensaje_resolucion_entrada(
                resolucion, error_inventario=self.error_inventario)
            estilo = "Status.TLabel"

        if estado.error:
            mensaje = estado.error
            estilo = "Error.Status.TLabel"
        self.estado_audio.set(mensaje)
        self.etiqueta_estado_audio.configure(style=estilo)
        self._actualizar_sucio()

    def _actualizar_dispositivos(self):
        if (self._cerrando or self._refresh_activo()
                or self.control_prueba.estado().fase != "idle"):
            return
        seleccion = self._audio_actual()

        def consultar():
            try:
                resultado = refrescar_entradas(
                    seleccion, self._listar_entradas)
            except Exception as exc:
                opciones = construir_opciones_entrada(seleccion, ())
                resultado = RefrescoEntradas(
                    inventario=(),
                    opciones=opciones,
                    indice_seleccionado=_indice_opcion_audio(
                        opciones, seleccion),
                    seleccion=seleccion,
                    error=str(exc),
                )
            self._refresh_audio_resultados.put(resultado)

        self._refresh_audio_thread = threading.Thread(
            target=consultar,
            name="settings-audio-refresh",
            daemon=True,
        )
        self._refresh_audio_thread.start()
        self._actualizar_audio()

    def _consumir_refresco_audio(self):
        try:
            resultado = self._refresh_audio_resultados.get_nowait()
        except queue.Empty:
            return
        hilo = self._refresh_audio_thread
        if hilo is not None and not hilo.is_alive():
            hilo.join()
        self._refresh_audio_thread = None
        if self._cerrando:
            return
        self.inventario = resultado.inventario
        self.error_inventario = resultado.error
        self.opciones_audio = resultado.opciones
        self.selector_audio.configure(
            values=tuple(opcion.etiqueta for opcion in self.opciones_audio))
        if resultado.indice_seleccionado >= 0:
            self.selector_audio.current(resultado.indice_seleccionado)
        self.control_prueba.actualizar_inventario(resultado.inventario)
        self._actualizar_audio()

    def _alternar_prueba_audio(self):
        estado = self.control_prueba.estado()
        if estado.fase == "active":
            self.control_prueba.detener()
        elif estado.fase == "idle":
            self.control_prueba.iniciar(self._audio_actual())
        self._actualizar_audio()

    def _programar_poll_audio(self):
        self._poll_audio_id = self.root.after(100, self._poll_audio)

    def _programar_poll_runtime(self):
        self._poll_runtime_id = self.root.after(500, self._poll_runtime)

    def _aplicar_estado_parlar(self, estado):
        self.estado_parlar = estado
        if not hasattr(self, "estado_runtime_titulo"):
            return
        self.estado_runtime_titulo.set(estado.titulo)
        self.estado_runtime_mensaje.set(estado.mensaje)
        estilo = {
            "ready": "Success.Status.TLabel",
            "paused": "Warning.Status.TLabel",
            "starting": "Warning.Status.TLabel",
            "attention": "Warning.Status.TLabel",
            "unavailable": "Error.Status.TLabel",
            "stopped": "Status.TLabel",
        }.get(estado.categoria, "Status.TLabel")
        if hasattr(self, "etiqueta_runtime_mensaje"):
            self.etiqueta_runtime_mensaje.configure(style=estilo)
        if hasattr(self, "boton_cambiar_atajo"):
            if estado.categoria == "starting" or self._cerrando:
                self.boton_cambiar_atajo.state(["disabled"])
            else:
                self.boton_cambiar_atajo.state(["!disabled"])

    def _presentar(self):
        try:
            if not self._puede_presentar():
                self._presentacion_pendiente = True
                return False
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except Exception:
            return False
        self._presentacion_visible = True
        self._presentacion_pendiente = False
        return True

    def _poll_runtime(self):
        self._poll_runtime_id = None
        if self._cerrando:
            return
        try:
            self._aplicar_estado_parlar(self._consultar_estado())
        except Exception:
            self._aplicar_estado_parlar(EstadoParlARSettings(
                "unavailable",
                "No disponible",
                "No se pudo actualizar el estado de ParlAR.",
                True,
            ))
        if (
            self._guardia_settings is not None
            and self._guardia_settings.consumir_presentacion()
        ):
            self._presentacion_pendiente = True
        if self._presentacion_pendiente:
            self._presentar()
        self._programar_poll_runtime()

    def _poll_audio(self):
        self._poll_audio_id = None
        self._consumir_refresco_audio()
        self._actualizar_audio()
        if (self._cerrando
                and self.control_prueba.estado().fase == "closed"
                and not self._refresh_activo()):
            self.control.cancelar(self.root.destroy)
            return
        self._programar_poll_audio()

    def _guardar(self):
        if self.control_prueba.estado().fase in {
                "starting", "stopping", "closing", "closed"}:
            return
        if not self.control.esta_sucio(self._valores()):
            return
        try:
            resultado = self.control.guardar(self._valores())
        except Exception as exc:
            self.estado.set(f"No se pudo guardar: {exc}")
            self.etiqueta_estado.configure(style="Error.Status.TLabel")
            return
        self.estado.set(mensaje_persistencia(resultado))
        self.etiqueta_estado.configure(style="Success.Status.TLabel")
        self._actualizar_sucio()

    def _al_escape(self, _evento=None):
        if self._modal_descarte is None:
            self._solicitar_cierre()
        return "break"

    def _solicitar_cierre(self):
        if self._cerrando:
            return
        if self._modal_atajo is not None:
            self._cerrar_captura_atajo()
        decision = accion_cierre_settings(
            sucio=self.control.esta_sucio(self._valores()))
        if decision == "confirmar":
            self._mostrar_confirmacion_descarte()
            return
        self._iniciar_cierre()

    def _mostrar_confirmacion_descarte(self):
        if self._modal_descarte is not None:
            self._modal_descarte.focus_force()
            return
        modal = self.tk.Toplevel(self.root)
        self._modal_descarte = modal
        modal.title("Cambios sin guardar")
        modal.resizable(False, False)
        modal.transient(self.root)
        modal.protocol("WM_DELETE_WINDOW", self._seguir_editando)
        modal.bind("<Escape>", lambda _evento: self._seguir_editando())
        cuerpo = self.ttk.Frame(modal, padding=20)
        cuerpo.grid(row=0, column=0, sticky="nsew")
        cuerpo.columnconfigure(0, weight=1)
        self.ttk.Label(
            cuerpo,
            text="Hay cambios sin guardar.",
            style="Section.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        self.ttk.Label(
            cuerpo,
            text="Podés seguir editando o descartar los cambios.",
            style="Status.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 18))
        seguir = self.ttk.Button(
            cuerpo, text="Seguir editando", command=self._seguir_editando)
        seguir.grid(row=2, column=0, padx=(0, 8))
        self.ttk.Button(
            cuerpo,
            text="Descartar cambios",
            command=self._descartar_confirmado,
        ).grid(row=2, column=1)
        modal.grab_set()
        seguir.focus_set()

    def _cerrar_modal_descarte(self):
        modal = self._modal_descarte
        self._modal_descarte = None
        if modal is not None:
            try:
                modal.grab_release()
                modal.destroy()
            except Exception:
                pass

    def _seguir_editando(self):
        self._cerrar_modal_descarte()
        self.root.focus_force()

    def _descartar_confirmado(self):
        self._cerrar_modal_descarte()
        self._iniciar_cierre()

    def _iniciar_cierre(self):
        if self._cerrando:
            return
        self._cerrando = True
        self._cerrar_captura_atajo()
        self.boton_cancelar.state(["disabled"])
        self.boton_guardar.state(["disabled"])
        self.boton_actualizar_audio.state(["disabled"])
        self.boton_cambiar_atajo.state(["disabled"])
        self.boton_restaurar_atajo.state(["disabled"])
        self.check_autostart.state(["disabled"])
        self.control_prueba.cerrar()
        self._actualizar_audio()

    def _cancelar(self):
        """Alias histórico para consumidores que cerraban la vista."""
        self._solicitar_cierre()


def main(
        *, consultar_estado: Callable | None = None,
        permitir_foco_inicial: bool = True,
        puede_presentar: Callable[[], bool] | None = None) -> int:
    from .launcher import GuardiaVentanaSettings

    guardia_settings = GuardiaVentanaSettings()
    try:
        if not guardia_settings.adquirir_o_solicitar_presentacion():
            return 0
        guardia_settings.instalar_receptor_presentacion()
    except Exception as exc:
        print(f"[settings] no se pudo coordinar la ventana: {exc}",
              file=sys.stderr)
        guardia_settings.liberar()
        return 1

    try:
        carga = cargar_configuracion_recuperable()
        base = carga.configuracion
        snapshot = snapshot_configuracion(base)
        capacidades = obtener_capacidades()
        inventario = cargar_inventario_entradas()
        proveedor_estado = consultar_estado or consultar_estado_parlar
        estado_parlar = proveedor_estado()
    except Exception as exc:
        print(f"[settings] no se pudo cargar la configuración: {exc}",
              file=sys.stderr)
        guardia_settings.liberar()
        return 2

    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception as exc:
        print(f"[settings] no se pudo abrir la ventana: {exc}",
              file=sys.stderr)
        guardia_settings.liberar()
        return 1

    try:
        VentanaSettings(
            root,
            ControlSettings(
                base,
                snapshot,
                requiere_reparacion=carga.advertencia is not None,
            ),
            capacidades,
            inventario=inventario.dispositivos,
            error_inventario=inventario.error,
            estado_parlar=estado_parlar,
            consultar_estado=proveedor_estado,
            guardia_settings=guardia_settings,
            permitir_foco_inicial=permitir_foco_inicial,
            puede_presentar=puede_presentar,
            advertencia_config=carga.advertencia,
        )
        root.mainloop()
    except Exception as exc:
        print(f"[settings] la ventana se cerró por un error: {exc}",
              file=sys.stderr)
        return 1
    finally:
        guardia_settings.liberar()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
