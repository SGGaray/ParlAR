"""Ventana Tkinter básica para editar Settings persistidos de ParlAR.

La lógica de formulario y persistencia permanece separada de Tk para que el
contrato pueda probarse sin display. Guardar nunca modifica una ``App`` activa:
los cambios se escriben para el próximo inicio mediante ``settings_backend``.
"""

import dataclasses
from pathlib import Path
import logging
import queue
import subprocess
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
    resolver_dispositivo_entrada,
    snapshot_configuracion,
)
from .ui_tema import PALETA, IndicadorEstado, Interruptor, Tema
from .restart import (
    EstadoResultadoReinicio,
    comando_reinicio,
    interpretar_resultado,
)


_LOG = logging.getLogger(__name__)


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
    guionar_exclusive_output: bool = True


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
                "hotkey_toggle", "mode", "rewrite_mode", "language",
                "context_terms",
            ),
            (
                "hotkey_toggle", "mode", "rewrite_mode", "language",
                "context_terms",
            ),
        ),
        PestañaSettings(
            "Micrófono",
            ("audio_input_device", "audio_refresh", "audio_test"),
            ("audio_input_device", "audio_refresh", "audio_test"),
        ),
        PestañaSettings(
            "GuionAR",
            ("guionar_status", "guionar", "guionar_exclusive_output"),
            ("guionar", "guionar_exclusive_output"),
        ),
        PestañaSettings(
            "Aplicación",
            ("overlay", "overlay_position", "autostart", "guardar_sesion"),
            ("overlay", "overlay_position", "autostart", "guardar_sesion"),
        ),
        PestañaSettings(
            "Avanzado",
            ("model_size", "device", "compute_type", "injector"),
            ("model_size", "device", "compute_type", "injector"),
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


def mensaje_autostart_inmediato() -> str:
    return "Este cambio se aplica de inmediato; no depende de Guardar cambios."


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
        _LOG.info("no se pudo obtener el inventario de audio: %s", exc)
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
        return (
            "No se pudo obtener la lista de micrófonos. "
            "Podés volver a intentarlo con Actualizar."
        )
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


def mensaje_error_prueba_audio(error: str | None) -> str:
    if not error:
        return ""
    return (
        "No se pudo completar la prueba de micrófono. "
        "Verificá que el dispositivo esté disponible y no esté en uso."
    )


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
        guionar_exclusive_output=snapshot.guionar_exclusive_output,
    )


def snapshot_desde_valores(
        inicial: SettingsSnapshot,
        valores: ValoresFormulario) -> SettingsSnapshot:
    datos = dataclasses.asdict(valores)
    datos["context_terms"] = parsear_context_terms(valores.context_terms)
    return dataclasses.replace(inicial, **datos)


def texto_estado_guionar(estado) -> tuple[str, bool]:
    """Texto breve de presencia de GuionAR y si está conectado."""
    if not getattr(estado, "ejecutandose", False):
        return "○ No detectado (ParlAR no está ejecutándose)", False
    guionar = getattr(estado, "guionar", None)
    if guionar == "connected":
        return "● Conectado", True
    if guionar == "disconnected":
        return "○ No detectado", False
    return "○ Integración desactivada", False


def _sin_marca(texto: str) -> str:
    """El punto de estado se dibuja aparte (relleno/anillo), no como glifo."""
    return texto[2:] if texto[:2] in ("● ", "○ ") else texto


def mensaje_persistencia(resultado: ResultadoPersistencia) -> str:
    if resultado.requires_restart:
        return (
            "Configuración guardada. "
            "Los cambios se aplicarán al reiniciar ParlAR."
        )
    return "Configuración guardada. No es necesario reiniciar ParlAR."


def mensaje_persistencia_contextual(
        resultado: ResultadoPersistencia, *, runtime_activo: bool) -> str:
    if not resultado.requires_restart:
        return mensaje_persistencia(resultado)
    if runtime_activo:
        return "Cambios guardados. Reiniciá ParlAR para aplicarlos."
    return "Cambios guardados. Se aplicarán al iniciar ParlAR."


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
        # Cualquier diferencia se puede guardar, incluso las que ParlAR
        # aplica en caliente (que no exigen reinicio).
        return (self.requiere_reparacion
                or self.snapshot_inicial != self.snapshot_candidato(valores))

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
            control_autostart: ControlAutostart | None = None,
            popen: Callable = subprocess.Popen):
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
        self._popen = popen
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
        self._modal_reinicio = None
        self._captura_atajo = None
        self._suspension_atajo = None
        self._renovacion_atajo_id = None
        self._boton_origen_atajo = None
        self._proceso_reinicio = None
        self._poll_reinicio_id = None
        self._reinicio_requerido = False
        self._gestor_reintento = None
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
        self._aplicar_icono_ventana()
        self.root.protocol("WM_DELETE_WINDOW", self._solicitar_cierre)
        self.root.bind("<Escape>", self._al_escape)
        self._configurar_estilos()
        self._crear_variables(
            valores_desde_snapshot(control.snapshot_inicial))
        self._crear_contenido()
        self._ajustar_geometria()
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
        self.tema = Tema(self.root)

    def _px(self, valor):
        return self.tema.px(valor)

    def _aplicar_icono_ventana(self):
        """Marca de ParlAR en la ventana: máster de 16 px para tamaños chicos
        y el app mark para el resto. Tk sin SVG: se omite sin error."""
        assets = Path(__file__).with_name("assets")
        try:
            imagenes = [
                self.tk.PhotoImage(master=self.root,
                                   file=str(assets / "brand" / "parlar-16.svg"),
                                   format="svg -scaletoheight 16"),
                self.tk.PhotoImage(master=self.root,
                                   file=str(assets / "parlar.svg"),
                                   format="svg -scaletoheight 64"),
            ]
            self.root.iconphoto(True, *imagenes)
            self._iconos_ventana = imagenes   # PhotoImage vive mientras la ref
        except Exception:
            self._iconos_ventana = []

    def _ajustar_geometria(self):
        """Tamaño compacto escalado por DPI y acotado a la pantalla."""
        ancho, alto = self._px(820), self._px(600)
        try:
            pantalla_w = self.root.winfo_screenwidth()
            pantalla_h = self.root.winfo_screenheight()
        except Exception:
            pantalla_w = pantalla_h = 0
        if pantalla_w and pantalla_h:
            ancho = min(ancho, int(pantalla_w * 0.92))
            alto = min(alto, int(pantalla_h * 0.9))
        self.root.geometry(f"{ancho}x{alto}")
        self.root.minsize(min(ancho, self._px(620)),
                          min(alto, self._px(440)))

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
            "guionar_exclusive_output": tk.BooleanVar(
                value=valores.guionar_exclusive_output),
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
        self.estado_guionar = tk.StringVar(
            value=texto_estado_guionar(self.estado_parlar)[0])
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
        e = self.tema.espacio
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        # Encabezado: qué es y cómo está ParlAR, siempre a la vista.
        header = ttk.Frame(self.root, style="Header.TFrame",
                           padding=(e("xl"), e("l"), e("xl"), e("m")))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="ParlAR", style="Titulo.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Label(header, text="Configuración",
                  style="Header.Status.TLabel").grid(
            row=0, column=1, sticky="sw", padx=(e("s"), 0), pady=(0, 2))
        estado = ttk.Frame(header, style="Header.TFrame")
        estado.grid(row=0, column=2, sticky="e")
        self.indicador_runtime = IndicadorEstado(
            estado, self.tema, PALETA.lateral)
        self.indicador_runtime.grid(row=0, column=0, padx=(0, e("s")))
        self.etiqueta_runtime_titulo = ttk.Label(
            estado, textvariable=self.estado_runtime_titulo,
            style="Estado.TLabel")
        self.etiqueta_runtime_titulo.grid(row=0, column=1, sticky="e")
        self.etiqueta_runtime_mensaje = ttk.Label(
            header, textvariable=self.estado_runtime_mensaje,
            style="Header.Status.TLabel", wraplength=self._px(640))
        self.etiqueta_runtime_mensaje.grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(e("xs"), 0))
        header.bind(
            "<Configure>", lambda ev: self.etiqueta_runtime_mensaje.configure(
                wraplength=max(self._px(200), ev.width - 2 * e("xl"))),
            add="+")
        ttk.Frame(self.root, style="Linea.TFrame", height=1).grid(
            row=0, column=0, sticky="sew")

        cuerpo = ttk.Frame(self.root)
        cuerpo.grid(row=1, column=0, sticky="nsew")
        cuerpo.columnconfigure(1, weight=1)
        cuerpo.rowconfigure(0, weight=1)

        # Navegación lateral compacta.
        self.lateral = ttk.Frame(cuerpo, style="Lateral.TFrame",
                                 padding=(e("s"), e("m"), e("s"), e("m")))
        self.lateral.grid(row=0, column=0, sticky="ns")
        ttk.Frame(cuerpo, style="Linea.TFrame", width=1).grid(
            row=0, column=0, sticky="nse")
        self.paginas = ttk.Frame(cuerpo)
        self.paginas.grid(row=0, column=1, sticky="nsew")
        self.paginas.columnconfigure(0, weight=1)
        self.paginas.rowconfigure(0, weight=1)

        self._navegacion = {}
        self._seccion_actual = None
        constructores = (
            ("Dictado", self._crear_dictado,
             "Atajo, estrategia e idioma del dictado."),
            ("Micrófono", self._crear_audio,
             "Qué entrada escucha ParlAR."),
            ("GuionAR", self._crear_guionar,
             "Teleprompter que sigue tu voz."),
            ("Aplicación", self._crear_aplicacion,
             "Indicador, inicio y privacidad."),
            ("Avanzado", self._crear_avanzado,
             "Modelo y método de escritura."),
        )
        for indice, (nombre, crear, descripcion) in enumerate(constructores):
            boton = ttk.Button(
                self.lateral, text=nombre, style="Nav.TButton",
                command=lambda n=nombre: self.seleccionar_seccion(n))
            boton.grid(row=indice, column=0, sticky="ew", pady=(0, 2))
            boton.bind("<Return>", lambda _e, n=nombre:
                       self._activar_navegacion(n), add="+")
            boton.bind("<Down>", lambda _e, i=indice: self._mover_nav(i, 1))
            boton.bind("<Up>", lambda _e, i=indice: self._mover_nav(i, -1))
            self._navegacion[nombre] = boton
            contenido = self._crear_pestana_scroll(nombre, descripcion)
            crear(contenido)
        self._orden_secciones = tuple(n for n, _c, _d in constructores)
        self.lateral.columnconfigure(0, minsize=self._px(150))

        # Pie fijo: estado de guardado y acciones.
        ttk.Frame(self.root, style="Linea.TFrame", height=1).grid(
            row=2, column=0, sticky="new")
        self.footer = ttk.Frame(
            self.root, padding=(e("xl"), e("m"), e("xl"), e("m")))
        self.footer.grid(row=2, column=0, sticky="ew")
        self.footer.columnconfigure(0, weight=1)
        self.etiqueta_estado = ttk.Label(
            self.footer,
            textvariable=self.estado,
            style="Status.TLabel",
            wraplength=self._px(380),
        )
        self.etiqueta_estado.grid(row=0, column=0, sticky="w")
        if self._advertencia_config:
            self.etiqueta_estado.configure(style="Error.Status.TLabel")
        self.boton_reiniciar = ttk.Button(
            self.footer,
            text="Reiniciar ParlAR",
            command=self._iniciar_reinicio,
        )
        self.boton_reiniciar.grid(row=0, column=1, padx=(e("m"), 0))
        self._vincular_enter(
            self.boton_reiniciar, self._iniciar_reinicio)
        self.boton_reiniciar.grid_remove()
        self.boton_cancelar = ttk.Button(
            self.footer,
            text="Cerrar",
            command=self._solicitar_cierre,
        )
        self.boton_cancelar.grid(row=0, column=2, padx=(e("s"), 0))
        self._vincular_enter(
            self.boton_cancelar, self._solicitar_cierre)
        self.boton_guardar = ttk.Button(
            self.footer, text="Guardar cambios", style="Primario.TButton",
            command=self._guardar)
        self.boton_guardar.grid(row=0, column=3, padx=(e("s"), 0))
        self._vincular_enter(self.boton_guardar, self._guardar)

        self.root.bind("<MouseWheel>", self._rueda_scroll, add="+")
        self.root.bind("<Button-4>", self._rueda_scroll, add="+")
        self.root.bind("<Button-5>", self._rueda_scroll, add="+")
        self.root.bind("<Prior>", self._pagina_scroll, add="+")
        self.root.bind("<Next>", self._pagina_scroll, add="+")
        for secuencia, delta in (("<Control-Tab>", 1),
                                 ("<Control-ISO_Left_Tab>", -1),
                                 ("<Control-Shift-Tab>", -1),
                                 ("<Control-Next>", 1),
                                 ("<Control-Prior>", -1)):
            self.root.bind(secuencia, lambda _e, d=delta:
                           self._ciclar_seccion(d), add="+")
        self.seleccionar_seccion(self._orden_secciones[0])
        self._aplicar_estado_parlar(self.estado_parlar)

    # ------------------------------------------------------- navegación
    def seleccionar_seccion(self, nombre):
        if nombre not in self._areas_scroll:
            return
        for otro, area in self._areas_scroll.items():
            if otro == nombre:
                area["pestana"].grid()
            else:
                area["pestana"].grid_remove()
        for otro, boton in self._navegacion.items():
            boton.state(["selected"] if otro == nombre else ["!selected"])
        self._seccion_actual = nombre

    @property
    def seccion_actual(self):
        return self._seccion_actual

    def _activar_navegacion(self, nombre):
        self.seleccionar_seccion(nombre)
        return "break"

    def _mover_nav(self, indice, delta):
        nombres = self._orden_secciones
        destino = nombres[(indice + delta) % len(nombres)]
        self._navegacion[destino].focus_set()
        self.seleccionar_seccion(destino)
        return "break"

    def _ciclar_seccion(self, delta):
        nombres = self._orden_secciones
        actual = nombres.index(self._seccion_actual)
        destino = nombres[(actual + delta) % len(nombres)]
        self.seleccionar_seccion(destino)
        self._navegacion[destino].focus_set()
        return "break"

    def _crear_pestana_scroll(self, nombre, descripcion=""):
        ttk = self.ttk
        e = self.tema.espacio
        pestana = ttk.Frame(self.paginas)
        pestana.columnconfigure(0, weight=1)
        pestana.rowconfigure(0, weight=1)
        pestana.grid(row=0, column=0, sticky="nsew")

        canvas = self.tk.Canvas(
            pestana, borderwidth=0, highlightthickness=0, takefocus=0,
            background=PALETA.fondo)
        barra = ttk.Scrollbar(
            pestana, orient="vertical", command=canvas.yview,
            style="Fina.Vertical.TScrollbar")
        canvas.configure(yscrollcommand=barra.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        barra.grid(row=0, column=1, sticky="ns", padx=(0, 2))

        contenido = ttk.Frame(
            canvas, padding=(e("xl"), e("l"), e("xl"), e("xl")))
        contenido.columnconfigure(0, weight=1)
        ttk.Label(contenido, text=nombre, style="Section.TLabel").grid(
            row=0, column=0, sticky="w")
        if descripcion:
            ttk.Label(contenido, text=descripcion,
                      style="Status.TLabel").grid(
                row=1, column=0, sticky="w", pady=(2, 0))
        contenido._siguiente_fila = 2
        ventana = canvas.create_window(
            (0, 0), window=contenido, anchor="nw")
        area = {
            "canvas": canvas,
            "contenido": contenido,
            "ventana": ventana,
            "pestana": pestana,
            "barra": barra,
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

    # ------------------------------------------------------- piezas
    def _siguiente_fila(self, contenido):
        fila = contenido._siguiente_fila
        contenido._siguiente_fila += 1
        return fila

    def _titulo_seccion(self, padre, texto, fila=None):
        """Subtítulo de grupo dentro de una sección."""
        if fila is None:
            fila = self._siguiente_fila(padre)
        etiqueta = self.ttk.Label(padre, text=texto, style="Grupo.TLabel")
        etiqueta.grid(row=fila, column=0, sticky="w",
                      pady=(self.tema.espacio("l"), self.tema.espacio("s")))
        return etiqueta

    def _grupo(self, contenido, titulo=None):
        if titulo:
            self._titulo_seccion(contenido, titulo)
        grupo = self.ttk.Frame(contenido, style="Grupo.TFrame", padding=1)
        grupo.grid(row=self._siguiente_fila(contenido), column=0,
                   sticky="ew", pady=(0 if titulo else self.tema.espacio("l"),
                                      0))
        grupo.columnconfigure(0, weight=1)
        grupo._filas = 0
        return grupo

    def _nota(self, contenido, texto=None, *, variable=None, estilo=None):
        """Texto secundario debajo de un grupo (estado o aclaración)."""
        opciones = dict(style=estilo or "Status.TLabel",
                        wraplength=self._px(560))
        if variable is not None:
            opciones["textvariable"] = variable
        else:
            opciones["text"] = texto
        etiqueta = self.ttk.Label(contenido, **opciones)
        etiqueta.grid(row=self._siguiente_fila(contenido), column=0,
                      sticky="w", pady=(self.tema.espacio("s"), 0))
        return etiqueta

    def _fila(self, grupo, titulo=None, ayuda=None):
        """Fila de un grupo: texto a la izquierda, control a la derecha.

        Devuelve el contenedor del control."""
        ttk = self.ttk
        e = self.tema.espacio
        if grupo._filas:
            ttk.Frame(grupo, style="Linea.TFrame", height=1).grid(
                row=grupo._filas * 2 - 1, column=0, sticky="ew",
                padx=(e("m"), 0))
        fila = ttk.Frame(grupo, style="Grupo.TFrame",
                         padding=(e("m"), e("s") + 2, e("m"), e("s") + 2))
        fila.grid(row=grupo._filas * 2, column=0, sticky="ew")
        fila.columnconfigure(0, weight=1)
        grupo._filas += 1
        if titulo is not None:
            ttk.Label(fila, text=titulo, style="Etiqueta.TLabel").grid(
                row=0, column=0, sticky="w")
        control = ttk.Frame(fila, style="Grupo.TFrame")
        control.grid(row=0, column=1, rowspan=2, sticky="e",
                     padx=(e("l"), 0))
        ayuda_lbl = None
        if ayuda:
            ayuda_lbl = ttk.Label(fila, text=ayuda, style="Ayuda.TLabel",
                                  wraplength=self._px(260))
            ayuda_lbl.grid(row=1, column=0, sticky="w", pady=(2, 0))
        estado = {"apilado": False}

        def ajustar(_evento, etiqueta=ayuda_lbl, fila=fila,
                    control=control):
            # Ventana angosta: el control baja debajo del texto en vez de
            # aplastarlo. La ayuda usa el ancho que queda, nunca lo pisa.
            ancho_fila = fila.winfo_width()
            libre = ancho_fila - control.winfo_reqwidth() - self._px(48)
            apilar = libre < self._px(170)
            if apilar != estado["apilado"]:
                estado["apilado"] = apilar
                if apilar:
                    control.grid_configure(row=2, column=0, rowspan=1,
                                           sticky="w", padx=0,
                                           pady=(e("s"), 0))
                else:
                    control.grid_configure(row=0, column=1, rowspan=2,
                                           sticky="e", padx=(e("l"), 0),
                                           pady=0)
            if etiqueta is not None:
                ancho = (ancho_fila - self._px(32) if apilar
                         else max(self._px(160), libre))
                if int(str(etiqueta.cget("wraplength")) or 0) != ancho:
                    etiqueta.configure(wraplength=ancho)

        fila.bind("<Configure>", ajustar, add="+")
        return control

    def _fila_interruptor(self, grupo, nombre_foco, texto, variable, *,
                          ayuda=None, command=None):
        """Toggle a lo ancho de una fila del grupo."""
        e = self.tema.espacio
        if grupo._filas:
            self.ttk.Frame(grupo, style="Linea.TFrame", height=1).grid(
                row=grupo._filas * 2 - 1, column=0, sticky="ew",
                padx=(e("m"), 0))
        interruptor = Interruptor(
            grupo, self.tema, text=texto, variable=variable, ayuda=ayuda,
            command=command)
        interruptor.fila.configure(padx=e("m"), pady=e("s") + 2)
        interruptor.grid(row=grupo._filas * 2, column=0, sticky="ew")
        grupo._filas += 1
        self._registrar_foco(nombre_foco, interruptor)
        return interruptor

    def _registrar_foco(self, nombre, widget):
        self._widgets_foco[nombre] = widget
        widget.bind(
            "<FocusIn>",
            lambda _evento, control=widget:
                self.root.after_idle(
                    lambda: self._asegurar_foco_visible(control)),
            add="+",
        )

    @staticmethod
    def _vincular_enter(boton, comando):
        def activar(_evento):
            comando()
            return "break"

        boton.bind("<Return>", activar)

    def _crear_dictado(self, contenido):
        ttk = self.ttk
        e = self.tema.espacio
        grupo = self._grupo(contenido, "Atajo")
        control = self._fila(
            grupo, "Atajo de dictado",
            "Mantenelo para hablar y soltalo para terminar. Doble toque: "
            "dictado continuo. Escape cancela lo pendiente.")
        self.etiqueta_hotkey = ttk.Label(
            control,
            textvariable=self.hotkey_etiqueta,
            anchor="center",
            style="Teclas.TLabel",
        )
        self.etiqueta_hotkey.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.boton_cambiar_atajo = ttk.Button(
            control,
            text="Cambiar…",
            command=self._mostrar_captura_atajo,
        )
        self.boton_cambiar_atajo.grid(row=1, column=0, sticky="ew",
                                      pady=(e("s"), 0))
        self._vincular_enter(
            self.boton_cambiar_atajo, self._mostrar_captura_atajo)
        self._registrar_foco("hotkey_toggle", self.boton_cambiar_atajo)
        self.boton_restaurar_atajo = ttk.Button(
            control,
            text="Predeterminado",
            command=self._restaurar_atajo,
        )
        self.boton_restaurar_atajo.grid(row=1, column=1, sticky="ew",
                                        padx=(e("s"), 0), pady=(e("s"), 0))
        self._vincular_enter(
            self.boton_restaurar_atajo, self._restaurar_atajo)

        grupo = self._grupo(contenido, "Reconocimiento")
        self._campo(
            grupo, "Estrategia", "mode",
            opciones=self.opciones_selectores["mode"],
            ayuda="Por frases espera una pausa; Incremental escribe "
                  "mientras hablás.")
        self._campo(
            grupo, "Reescritura", "rewrite_mode",
            opciones=self.opciones_selectores["rewrite_mode"],
            ayuda="Ajusta el estilo del texto después de reconocerlo.")
        control = self._fila(
            grupo, "Idioma",
            "Código como es o en. Vacío detecta el idioma.")
        idioma = ttk.Entry(
            control, textvariable=self.variables["language"], width=8)
        idioma.grid(row=0, column=0, sticky="e")
        self._registrar_foco("language", idioma)

        grupo = self._grupo(contenido, "Palabras y nombres")
        fila = self._fila(
            grupo, None,
            "Nombres propios, siglas o términos que ParlAR debería "
            "reconocer mejor. Uno por línea.")
        fila.master.columnconfigure(0, weight=1)
        self.contexto = self.tk.Text(
            fila.master,
            height=5,
            width=40,
            wrap="word",
            undo=True,
            **self.tema.estilo_texto_libre(),
        )
        self.contexto.grid(
            row=2, column=0, columnspan=2, sticky="nsew", pady=(e("s"), 0))
        self.contexto.insert("1.0", self._contexto_inicial)
        self.contexto.edit_modified(False)
        self._registrar_foco("context_terms", self.contexto)

    def _crear_audio(self, contenido):
        ttk = self.ttk
        e = self.tema.espacio
        grupo = self._grupo(contenido, "Entrada")
        control = self._fila(grupo, "Micrófono")
        self.selector_audio = ttk.Combobox(
            control,
            textvariable=self.audio_seleccion,
            values=tuple(opcion.etiqueta for opcion in self.opciones_audio),
            state="readonly",
            width=26,
        )
        self.selector_audio.grid(row=0, column=0, sticky="e")
        self._registrar_foco("audio_input_device", self.selector_audio)
        if self._indice_audio_inicial >= 0:
            self.selector_audio.current(self._indice_audio_inicial)
        self.boton_actualizar_audio = ttk.Button(
            control,
            text="Actualizar",
            command=self._actualizar_dispositivos,
        )
        self.boton_actualizar_audio.grid(row=0, column=1, padx=(e("s"), 0))
        self._vincular_enter(
            self.boton_actualizar_audio, self._actualizar_dispositivos)
        self._registrar_foco("audio_refresh", self.boton_actualizar_audio)

        control = self._fila(
            grupo, "Prueba", "Escuchá el nivel antes de dictar. "
            "El audio no se guarda.")
        self.medidor_audio = ttk.Progressbar(
            control, maximum=100, mode="determinate",
            length=self._px(140), style="Medidor.Horizontal.TProgressbar")
        self.medidor_audio.grid(row=0, column=0, sticky="ew",
                                padx=(0, e("m")))
        self.boton_prueba = ttk.Button(
            control,
            text="Probar micrófono",
            command=self._alternar_prueba_audio,
        )
        self.boton_prueba.grid(row=0, column=1)
        self._vincular_enter(
            self.boton_prueba, self._alternar_prueba_audio)
        self._registrar_foco("audio_test", self.boton_prueba)
        self.etiqueta_estado_audio = self._nota(
            contenido, variable=self.estado_audio)

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
        self._superficie_captura_atajo = self.ttk.Entry(
            cuerpo,
            textvariable=self._captura_atajo_texto,
            justify="center",
            state="readonly",
        )
        self._superficie_captura_atajo.grid(
            row=2, column=0, columnspan=2, sticky="ew", ipady=8)
        self.ttk.Label(
            cuerpo,
            textvariable=self._captura_atajo_error,
            style="Error.Status.TLabel",
            wraplength=430,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 14))

        cancelar = self.ttk.Button(
            cuerpo, text="Cancelar", command=self._cancelar_captura_atajo)
        cancelar.grid(row=4, column=0, sticky="e", padx=(0, 8))
        self._vincular_enter(cancelar, self._cancelar_captura_atajo)
        self._boton_usar_atajo = self.ttk.Button(
            cuerpo, text="Usar combinación", style="Primario.TButton",
            command=self._usar_captura_atajo)
        self._boton_usar_atajo.grid(row=4, column=1, sticky="e")
        self._vincular_enter(
            self._boton_usar_atajo, self._usar_captura_atajo)
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

    def _crear_guionar(self, contenido):
        ttk = self.ttk
        e = self.tema.espacio
        estado = ttk.Frame(contenido)
        estado.grid(row=self._siguiente_fila(contenido), column=0,
                    sticky="w", pady=(e("m"), 0))
        texto, conectado = texto_estado_guionar(self.estado_parlar)
        self.indicador_guionar = IndicadorEstado(estado, self.tema,
                                                 PALETA.fondo)
        self.indicador_guionar.grid(row=0, column=0, padx=(0, e("s")))
        self.estado_guionar.set(_sin_marca(texto))
        self.etiqueta_estado_guionar = ttk.Label(
            estado,
            textvariable=self.estado_guionar,
            style="Success.Status.TLabel" if conectado else "Status.TLabel",
        )
        self.etiqueta_estado_guionar.grid(row=0, column=1, sticky="w")
        self._pintar_indicador_guionar(conectado)

        grupo = self._grupo(contenido)
        self.check_guionar = self._fila_interruptor(
            grupo, "guionar", "Integrar con GuionAR",
            self.variables["guionar"],
            ayuda="Cuando GuionAR está abierto, ParlAR puede enviarle el "
                  "dictado automáticamente.")
        self.check_guionar_exclusivo = self._fila_interruptor(
            grupo, "guionar_exclusive_output",
            "Usar GuionAR como salida exclusiva",
            self.variables["guionar_exclusive_output"],
            ayuda="Cuando GuionAR esté conectado, ParlAR no escribirá el "
                  "dictado en otras aplicaciones.")
        self._nota(contenido, "La salida exclusiva se aplica desde la "
                              "siguiente frase, sin reiniciar.")

    def _pintar_indicador_guionar(self, conectado):
        if not hasattr(self, "indicador_guionar"):
            return
        self.indicador_guionar.fijar(
            "ready" if conectado else "stopped", lleno=conectado)

    def _crear_aplicacion(self, contenido):
        grupo = self._grupo(contenido, "Indicador")
        self.check_overlay = self._fila_interruptor(
            grupo, "overlay", "Mostrar indicador durante el dictado",
            self.variables["overlay"])
        self._campo(
            grupo,
            "Posición",
            "overlay_position",
            opciones=self.opciones_selectores["overlay_position"],
            ayuda="Dónde aparece la señal visual mientras dictás.",
        )

        grupo = self._grupo(contenido, "Inicio")
        self.check_autostart = self._fila_interruptor(
            grupo, "autostart", "Iniciar ParlAR al iniciar sesión",
            self.autostart_activado, command=self._alternar_autostart,
            ayuda=mensaje_autostart_inmediato())
        self.etiqueta_autostart = self._nota(
            contenido, variable=self.estado_autostart)
        self._aplicar_estado_autostart(self.control_autostart.estado)

        grupo = self._grupo(contenido, "Privacidad")
        self.check_guardar_sesion = self._fila_interruptor(
            grupo, "guardar_sesion", "Conservar transcripciones",
            self.variables["guardar_sesion"],
            ayuda="Guarda una copia local de cada dictado confirmado. "
                  "Apagado por defecto.")

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
        grupo = self._grupo(contenido, "Reconocimiento")
        self._campo(
            grupo, "Modelo", "model_size",
            ayuda="Tamaño del modelo de voz, por ejemplo small o medium.")
        self._campo(
            grupo, "Acelerador", "device",
            opciones=self.opciones_selectores["device"],
            ayuda="Automático elige CPU o NVIDIA CUDA al iniciar.")
        self._campo(
            grupo, "Precisión de cálculo", "compute_type",
            opciones=self.opciones_selectores["compute_type"],
            ayuda="Formato numérico del modelo de reconocimiento.")

        grupo = self._grupo(contenido, "Salida")
        self._campo(
            grupo, "Método de escritura", "injector",
            opciones=self.opciones_selectores["injector"],
            ayuda="Cómo se entrega el texto a la aplicación con foco.")
        self._nota(contenido, mensaje_reinicio_previo())

    def _actualizar_region_scroll(self, canvas):
        region = canvas.bbox("all")
        if region is not None:
            canvas.configure(scrollregion=region)
        # La barra sólo aparece si el contenido no entra.
        for area in self._areas_scroll.values():
            if area["canvas"] is canvas and "barra" in area:
                sobra = (area["contenido"].winfo_reqheight()
                         <= max(1, canvas.winfo_height()))
                if sobra:
                    area["barra"].grid_remove()
                    canvas.yview_moveto(0)
                else:
                    area["barra"].grid()

    def _ajustar_ancho_contenido(self, evento, canvas, ventana):
        canvas.itemconfigure(ventana, width=evento.width)
        self._actualizar_region_scroll(canvas)

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
            self, grupo, etiqueta, nombre, *, opciones=None, ayuda=None,
            ancho=22):
        control = self._fila(grupo, etiqueta, ayuda)
        if opciones is None:
            widget = self.ttk.Entry(
                control, textvariable=self.variables[nombre], width=ancho)
        else:
            widget = self.ttk.Combobox(
                control,
                textvariable=self.variables[nombre],
                values=tuple(opcion.etiqueta for opcion in opciones),
                state="readonly",
                width=ancho,
            )
            indice = indice_opcion_selector(
                opciones, self.variables[nombre].get())
            if indice >= 0:
                widget.current(indice)
            self.selectores[nombre] = (widget, opciones)
        widget.grid(row=0, column=0, sticky="e")
        self._registrar_foco(nombre, widget)
        return widget

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
        if not hasattr(self, "check_guionar"):
            return
        # Sólo tiene efecto con la integración activa; el valor se conserva.
        self.check_guionar_exclusivo.state(
            ["!disabled"] if self.variables["guionar"].get() else ["disabled"])
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
            guionar_exclusive_output=self.variables[
                "guionar_exclusive_output"].get(),
            # Sin campo visible: la ruta personalizada (CLI/config) se
            # conserva tal cual al guardar.
            guionar_socket=self.control.snapshot_inicial.guionar_socket,
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
            mensaje = mensaje_error_prueba_audio(estado.error)
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
                _LOG.exception("no se pudo actualizar el inventario de audio")
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
        self._acelerar_poll_audio()
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
        self._acelerar_poll_audio()

    def _acelerar_poll_audio(self):
        """Pasa al ritmo rápido ya, sin esperar el próximo poll de reposo."""
        pendiente, self._poll_audio_id = self._poll_audio_id, None
        if pendiente is not None:
            try:
                self.root.after_cancel(pendiente)
            except Exception:
                pass
        if not self._cerrando or pendiente is not None:
            self._poll_audio_id = self.root.after(100, self._poll_audio)

    def _programar_poll_audio(self):
        # Rápido sólo mientras el medidor o el inventario cambian; en reposo
        # el formulario ya se actualiza por eventos (trazas de variables).
        activo = (self._cerrando or self._refresh_activo()
                  or self.control_prueba.estado().fase != "idle")
        self._poll_audio_id = self.root.after(
            100 if activo else 500, self._poll_audio)

    def _programar_poll_runtime(self):
        self._poll_runtime_id = self.root.after(500, self._poll_runtime)

    def _aplicar_estado_parlar(self, estado):
        self.estado_parlar = estado
        if not hasattr(self, "estado_runtime_titulo"):
            return
        # El poll corre cada 500 ms: sólo se toca la UI si algo cambió.
        if self.estado_runtime_titulo.get() != estado.titulo:
            self.estado_runtime_titulo.set(estado.titulo)
        if self.estado_runtime_mensaje.get() != estado.mensaje:
            self.estado_runtime_mensaje.set(estado.mensaje)
        estilo = {
            "attention": "Warning.Header.Status.TLabel",
            "unavailable": "Error.Header.Status.TLabel",
        }.get(estado.categoria, "Header.Status.TLabel")
        if (hasattr(self, "etiqueta_runtime_mensaje")
                and str(self.etiqueta_runtime_mensaje.cget("style")) != estilo):
            self.etiqueta_runtime_mensaje.configure(style=estilo)
        indicador = getattr(self, "indicador_runtime", None)
        if indicador is not None and indicador.categoria != estado.categoria:
            indicador.fijar(estado.categoria)
        if hasattr(self, "etiqueta_estado_guionar"):
            texto, conectado = texto_estado_guionar(estado)
            texto = _sin_marca(texto)
            if self.estado_guionar.get() != texto:
                self.estado_guionar.set(texto)
                self.etiqueta_estado_guionar.configure(
                    style="Success.Status.TLabel" if conectado
                    else "Status.TLabel")
                self._pintar_indicador_guionar(conectado)
        if hasattr(self, "boton_cambiar_atajo"):
            if estado.categoria == "starting" or self._cerrando:
                self.boton_cambiar_atajo.state(["disabled"])
            else:
                self.boton_cambiar_atajo.state(["!disabled"])
        if hasattr(self, "boton_reiniciar"):
            self._actualizar_boton_reinicio()

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
        self._reinicio_requerido = resultado.requires_restart
        self.estado.set(mensaje_persistencia_contextual(
            resultado,
            runtime_activo=self.estado_parlar.ejecutandose,
        ))
        self.etiqueta_estado.configure(style="Success.Status.TLabel")
        self._actualizar_boton_reinicio()
        self._actualizar_sucio()

    def _actualizar_boton_reinicio(self):
        if not hasattr(self, "boton_reiniciar"):
            return
        en_curso = (
            self._proceso_reinicio is not None
            and self._proceso_reinicio.poll() is None
        )
        visible = (
            self._reinicio_requerido
            and (
                self.estado_parlar.ejecutandose
                or self._gestor_reintento is not None
            )
        )
        if visible:
            self.boton_reiniciar.grid()
            self.boton_reiniciar.configure(
                text=(
                    "Reiniciando…"
                    if en_curso
                    else "Reintentar reinicio"
                    if self._gestor_reintento is not None
                    else "Reiniciar ParlAR"
                ))
            self.boton_reiniciar.state(
                ["disabled"] if en_curso or self._cerrando
                else ["!disabled"])
        else:
            self.boton_reiniciar.grid_remove()

    def _iniciar_reinicio(self, *, terminar_dictado: bool = False):
        if self._cerrando:
            return
        if (
            self._proceso_reinicio is not None
            and self._proceso_reinicio.poll() is None
        ):
            return
        try:
            self._proceso_reinicio = self._popen(
                comando_reinicio(
                    terminar_dictado=terminar_dictado,
                    reintentar_gestor=self._gestor_reintento,
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )
        except OSError:
            self._proceso_reinicio = None
            self.estado.set("No se pudo reiniciar ParlAR.")
            self.etiqueta_estado.configure(style="Error.Status.TLabel")
            return
        self.estado.set("Reiniciando ParlAR…")
        self.etiqueta_estado.configure(style="Status.TLabel")
        self._actualizar_boton_reinicio()
        self._programar_poll_reinicio()

    def _programar_poll_reinicio(self):
        self._poll_reinicio_id = self.root.after(
            100, self._poll_reinicio)

    def _poll_reinicio(self):
        self._poll_reinicio_id = None
        proceso = self._proceso_reinicio
        if proceso is None:
            return
        if proceso.poll() is None:
            self._programar_poll_reinicio()
            return
        self._proceso_reinicio = None
        try:
            salida = proceso.stdout.read() if proceso.stdout is not None else ""
            resultado = interpretar_resultado(salida.strip())
        except (OSError, ValueError):
            resultado = None
        finally:
            if proceso.stdout is not None:
                proceso.stdout.close()

        if resultado is None:
            self._mostrar_fallo_reinicio(
                "No se pudo reiniciar ParlAR.")
        elif resultado.estado == EstadoResultadoReinicio.LISTO:
            self._reinicio_requerido = False
            self._gestor_reintento = None
            self.estado.set("Listo")
            self.etiqueta_estado.configure(style="Success.Status.TLabel")
        elif resultado.estado == EstadoResultadoReinicio.REQUIERE_CONFIRMACION:
            self._mostrar_confirmacion_reinicio()
        elif resultado.estado == EstadoResultadoReinicio.YA_EN_CURSO:
            self.estado.set("ParlAR ya se está reiniciando.")
            self.etiqueta_estado.configure(style="Status.TLabel")
        else:
            if resultado.gestor is not None:
                self._gestor_reintento = resultado.gestor
            self._mostrar_fallo_reinicio(resultado.mensaje)
        self._actualizar_boton_reinicio()

    def _mostrar_fallo_reinicio(self, detalle: str):
        mensaje = "No se pudo reiniciar ParlAR."
        if detalle and detalle != mensaje:
            mensaje += f" {detalle}"
        self.estado.set(mensaje)
        self.etiqueta_estado.configure(style="Error.Status.TLabel")

    def _mostrar_confirmacion_reinicio(self):
        if self._modal_reinicio is not None:
            return
        modal = self.tk.Toplevel(self.root)
        self._modal_reinicio = modal
        modal.title("Reiniciar ParlAR")
        modal.resizable(False, False)
        modal.transient(self.root)
        modal.protocol("WM_DELETE_WINDOW", self._cancelar_reinicio_dictado)
        cuerpo = self.ttk.Frame(modal, padding=20)
        cuerpo.grid(row=0, column=0, sticky="nsew")
        self.ttk.Label(
            cuerpo,
            text="Hay un dictado en curso.",
            style="Section.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        self.ttk.Label(
            cuerpo,
            text="Podés cancelar o terminar el dictado antes de reiniciar.",
            style="Status.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 18))
        cancelar = self.ttk.Button(
            cuerpo, text="Cancelar", command=self._cancelar_reinicio_dictado)
        cancelar.grid(row=2, column=0, padx=(0, 8))
        terminar = self.ttk.Button(
            cuerpo,
            text="Terminar y reiniciar",
            style="Primario.TButton",
            command=self._terminar_y_reiniciar,
        )
        terminar.grid(row=2, column=1)
        modal.grab_set()
        cancelar.focus_set()

    def _cerrar_modal_reinicio(self):
        modal, self._modal_reinicio = self._modal_reinicio, None
        if modal is not None:
            try:
                modal.grab_release()
                modal.destroy()
            except Exception:
                pass

    def _cancelar_reinicio_dictado(self):
        self._cerrar_modal_reinicio()
        self.estado.set(
            "Cambios guardados. Reiniciá ParlAR para aplicarlos.")
        self.etiqueta_estado.configure(style="Success.Status.TLabel")
        self._actualizar_boton_reinicio()

    def _terminar_y_reiniciar(self):
        self._cerrar_modal_reinicio()
        self._iniciar_reinicio(terminar_dictado=True)

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
        self._vincular_enter(seguir, self._seguir_editando)
        descartar = self.ttk.Button(
            cuerpo,
            text="Descartar cambios",
            style="Peligro.TButton",
            command=self._descartar_confirmado,
        )
        descartar.grid(row=2, column=1)
        self._vincular_enter(descartar, self._descartar_confirmado)
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
        self._cerrar_modal_reinicio()
        self._cerrar_captura_atajo()
        self.boton_cancelar.state(["disabled"])
        self.boton_guardar.state(["disabled"])
        self.boton_actualizar_audio.state(["disabled"])
        self.boton_cambiar_atajo.state(["disabled"])
        self.boton_restaurar_atajo.state(["disabled"])
        self.boton_reiniciar.state(["disabled"])
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
