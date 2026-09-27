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
from .settings_backend import (
    DispositivoEntrada,
    ErrorDispositivosAudio,
    ResolucionEntrada,
    ResultadoPersistencia,
    SettingsCapabilities,
    SettingsSnapshot,
    cargar_configuracion_actual,
    construir_configuracion_candidata,
    listar_dispositivos_entrada,
    obtener_capacidades,
    persistir_configuracion,
    resolver_dispositivo_entrada,
    requiere_reinicio,
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
        "streaming": "Continuo (streaming)",
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
    return "Los cambios de configuración se aplican al reiniciar ParlAR."


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
        with self._cv:
            if self._fase != "idle":
                return False
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
            persistir: Callable | None = None):
        self.configuracion_base = configuracion_base
        self.snapshot_inicial = snapshot_inicial
        self._persistir = persistir or persistir_configuracion

    def snapshot_candidato(
            self, valores: ValoresFormulario) -> SettingsSnapshot:
        return snapshot_desde_valores(self.snapshot_inicial, valores)

    def esta_sucio(self, valores: ValoresFormulario) -> bool:
        return requiere_reinicio(
            self.snapshot_inicial,
            self.snapshot_candidato(valores),
        )

    def guardar(self, valores: ValoresFormulario) -> ResultadoPersistencia:
        candidata = self.snapshot_candidato(valores)
        construir_configuracion_candidata(
            self.configuracion_base,
            candidata,
        )
        resultado = self._persistir(self.configuracion_base, candidata)
        self.snapshot_inicial = resultado.snapshot
        return resultado

    def cancelar(self, cerrar: Callable[[], None]) -> None:
        cerrar()


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
            control_prueba: ControlPruebaMicrofono | None = None):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.control = control
        self.capacidades = capacidades
        self.inventario = tuple(inventario)
        self.error_inventario = error_inventario
        self.control_prueba = control_prueba or ControlPruebaMicrofono(
            self.inventario)
        self._creando = True
        self._cerrando = False
        self._poll_audio_id = None

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

        self.root.title("ParlAR Settings")
        self.root.geometry("700x700")
        self.root.minsize(640, 600)
        self.root.protocol("WM_DELETE_WINDOW", self._cancelar)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        self._configurar_estilos()
        self._crear_variables(
            valores_desde_snapshot(control.snapshot_inicial))
        self._crear_contenido()
        self._conectar_cambios()
        self._creando = False
        self._actualizar_audio()
        self._actualizar_sucio()
        self._programar_poll_audio()

    def _configurar_estilos(self):
        estilo = self.ttk.Style(self.root)
        estilo.configure("Status.TLabel", foreground="#4b5563")
        estilo.configure("Success.Status.TLabel", foreground="#166534")
        estilo.configure("Error.Status.TLabel", foreground="#b91c1c")

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
        self.estado = tk.StringVar(value="")
        self.audio_seleccion = tk.StringVar(value="")
        self.estado_audio = tk.StringVar(value="")
        self._contexto_inicial = valores.context_terms

    def _crear_contenido(self):
        ttk = self.ttk
        zona_scroll = ttk.Frame(self.root)
        zona_scroll.grid(row=0, column=0, sticky="nsew")
        zona_scroll.columnconfigure(0, weight=1)
        zona_scroll.rowconfigure(0, weight=1)

        self.canvas_contenido = self.tk.Canvas(
            zona_scroll,
            borderwidth=0,
            highlightthickness=0,
        )
        barra_vertical = ttk.Scrollbar(
            zona_scroll,
            orient="vertical",
            command=self.canvas_contenido.yview,
        )
        self.canvas_contenido.configure(
            yscrollcommand=barra_vertical.set)
        self.canvas_contenido.grid(row=0, column=0, sticky="nsew")
        barra_vertical.grid(row=0, column=1, sticky="ns")

        contenedor = ttk.Frame(self.canvas_contenido, padding=16)
        contenedor.columnconfigure(0, weight=1)
        self._ventana_contenido = self.canvas_contenido.create_window(
            (0, 0),
            window=contenedor,
            anchor="nw",
        )
        contenedor.bind("<Configure>", self._actualizar_region_scroll)
        self.canvas_contenido.bind(
            "<Configure>", self._ajustar_ancho_contenido)

        general = ttk.LabelFrame(contenedor, text="General", padding=10)
        general.grid(row=0, column=0, sticky="ew")
        for columna in (1, 3):
            general.columnconfigure(columna, weight=1)

        self._campo(
            general, "Modelo Whisper", "model_size", 0, 0,
            ayuda="Nombre del modelo de transcripción, por ejemplo small o medium.")
        self._campo(
            general, "Acelerador", "device", 0, 2,
            opciones=self.opciones_selectores["device"],
            ayuda="Dónde se ejecutará Whisper; Automático elige al iniciar.")
        self._campo(
            general, "Formato de cálculo", "compute_type", 2, 0,
            opciones=self.opciones_selectores["compute_type"],
            ayuda="Precisión numérica usada por el motor de transcripción.")
        self._campo(
            general, "Idioma de dictado", "language", 2, 2,
            ayuda="Código de idioma, por ejemplo es o en; vacío detecta automáticamente.")
        self._campo(
            general, "Transcripción", "mode", 4, 0,
            opciones=self.opciones_selectores["mode"],
            ayuda="Por frases espera una pausa; Continuo actualiza mientras hablás.")
        self._campo(
            general, "Estilo del texto", "rewrite_mode", 4, 2,
            opciones=self.opciones_selectores["rewrite_mode"],
            ayuda="Ajusta el texto reconocido sin modificar el audio.")

        audio = ttk.LabelFrame(contenedor, text="Entrada de audio", padding=10)
        audio.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        audio.columnconfigure(1, weight=1)
        ttk.Label(audio, text="Micrófono").grid(
            row=0, column=0, padx=(0, 12), sticky="w")
        self.selector_audio = ttk.Combobox(
            audio,
            textvariable=self.audio_seleccion,
            values=tuple(opcion.etiqueta for opcion in self.opciones_audio),
            state="readonly",
        )
        self.selector_audio.grid(row=0, column=1, sticky="ew")
        if self._indice_audio_inicial >= 0:
            self.selector_audio.current(self._indice_audio_inicial)
        self.boton_prueba = ttk.Button(
            audio,
            text="Probar micrófono",
            command=self._alternar_prueba_audio,
        )
        self.boton_prueba.grid(row=0, column=2, padx=(10, 0))
        self.medidor_audio = ttk.Progressbar(
            audio, maximum=100, mode="determinate", length=140)
        self.medidor_audio.grid(
            row=1, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.etiqueta_estado_audio = ttk.Label(
            audio,
            textvariable=self.estado_audio,
            style="Status.TLabel",
            wraplength=620,
        )
        self.etiqueta_estado_audio.grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))

        salida = ttk.LabelFrame(contenedor, text="Salida", padding=10)
        salida.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        salida.columnconfigure(1, weight=1)
        self._campo(
            salida, "Método de escritura", "injector", 0, 0,
            opciones=self.opciones_selectores["injector"],
            ayuda="Cómo se entrega el texto a la aplicación con foco.")
        ttk.Checkbutton(
            salida, text="Usar GuionAR",
            variable=self.variables["guionar"],
        ).grid(row=0, column=2, padx=(18, 0), sticky="w")
        ttk.Checkbutton(
            salida, text="Guardar sesión",
            variable=self.variables["guardar_sesion"],
        ).grid(row=0, column=3, padx=(18, 0), sticky="w")

        interfaz = ttk.LabelFrame(
            contenedor, text="Interfaz / Indicador", padding=10)
        interfaz.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        interfaz.columnconfigure(1, weight=1)
        ttk.Checkbutton(
            interfaz, text="Mostrar overlay de dictado",
            variable=self.variables["overlay"],
        ).grid(row=0, column=0, sticky="w")
        self._campo(
            interfaz,
            "Posición del indicador",
            "overlay_position",
            1,
            0,
            opciones=self.opciones_selectores["overlay_position"],
            ayuda="Se aplica al reiniciar ParlAR.",
        )

        atajo = ttk.LabelFrame(contenedor, text="Atajo", padding=10)
        atajo.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        atajo.columnconfigure(1, weight=1)
        ttk.Label(atajo, text="Combinación").grid(
            row=0, column=0, padx=(0, 12), sticky="w")
        ttk.Entry(
            atajo, textvariable=self.variables["hotkey_toggle"],
        ).grid(row=0, column=1, sticky="ew")
        ttk.Label(
            atajo,
            text="Formato textual del atajo actual; todavía no captura teclas.",
            style="Status.TLabel",
        ).grid(row=1, column=1, pady=(6, 0), sticky="w")

        contexto = ttk.LabelFrame(contenedor, text="Contexto", padding=10)
        contexto.grid(row=5, column=0, sticky="nsew", pady=(10, 0))
        contexto.columnconfigure(0, weight=1)
        ttk.Label(
            contexto,
            text=(
                "Un término por línea. Se usan como contexto para mejorar "
                "el reconocimiento; las líneas vacías se ignoran."
            ),
            style="Status.TLabel",
            wraplength=600,
        ).grid(row=0, column=0, sticky="w")
        self.contexto = self.tk.Text(
            contexto,
            height=3,
            wrap="word",
            undo=True,
            relief="solid",
            borderwidth=1,
        )
        self.contexto.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self.contexto.insert("1.0", self._contexto_inicial)
        self.contexto.edit_modified(False)

        socket = ttk.LabelFrame(
            contenedor, text="Socket GuionAR", padding=10)
        socket.grid(row=6, column=0, sticky="ew", pady=(10, 0))
        socket.columnconfigure(1, weight=1)
        ttk.Label(socket, text="Ruta").grid(
            row=0, column=0, padx=(0, 12), sticky="w")
        ttk.Entry(
            socket, textvariable=self.variables["guionar_socket"],
        ).grid(row=0, column=1, sticky="ew")

        pie = ttk.Frame(contenedor)
        pie.grid(row=7, column=0, sticky="ew", pady=(14, 0))
        pie.columnconfigure(0, weight=1)
        ttk.Label(
            pie,
            text=mensaje_reinicio_previo(),
            style="Status.TLabel",
            wraplength=500,
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        self.etiqueta_estado = ttk.Label(
            pie,
            textvariable=self.estado,
            style="Status.TLabel",
            wraplength=400,
        )
        self.etiqueta_estado.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.boton_cancelar = ttk.Button(
            pie, text="Cancelar", command=self._cancelar,
        )
        self.boton_cancelar.grid(
            row=1, column=1, padx=(12, 8), pady=(8, 0))
        self.boton_guardar = ttk.Button(
            pie, text="Guardar", command=self._guardar)
        self.boton_guardar.grid(row=1, column=2, pady=(8, 0))

    def _actualizar_region_scroll(self, _evento=None):
        region = self.canvas_contenido.bbox("all")
        if region is not None:
            self.canvas_contenido.configure(scrollregion=region)

    def _ajustar_ancho_contenido(self, evento):
        self.canvas_contenido.itemconfigure(
            self._ventana_contenido,
            width=evento.width,
        )

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
            padx=(0, 14), pady=5, sticky="ew")
        if ayuda:
            self.ttk.Label(
                padre,
                text=ayuda,
                style="Status.TLabel",
                wraplength=270,
            ).grid(
                row=fila + 1,
                column=columna,
                columnspan=2,
                padx=(0, 14),
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
        self._actualizar_sucio()

    def _al_cambiar_audio(self, *_args):
        self._actualizar_audio()
        self._actualizar_sucio()

    def _al_modificar_contexto(self, _evento):
        if self.contexto.edit_modified():
            self.contexto.edit_modified(False)
            self._actualizar_sucio()

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
        if guardado_habilitado(
                sucio=self.control.esta_sucio(self._valores()),
                fase_audio=fase,
                cerrando=self._cerrando):
            self.boton_guardar.state(["!disabled"])
        else:
            self.boton_guardar.state(["disabled"])

    def _actualizar_audio(self):
        estado = self.control_prueba.estado()
        self.medidor_audio["value"] = estado.nivel * 100
        ocupado = estado.fase in {
            "starting", "active", "stopping", "closing", "closed"
        }
        self.selector_audio.configure(
            state="disabled" if ocupado else "readonly")

        if self._cerrando:
            self.boton_prueba.configure(text="Probar micrófono")
            self.boton_prueba.state(["disabled"])
            mensaje = "Cerrando la prueba de audio…"
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

    def _alternar_prueba_audio(self):
        estado = self.control_prueba.estado()
        if estado.fase == "active":
            self.control_prueba.detener()
        elif estado.fase == "idle":
            self.control_prueba.iniciar(self._audio_actual())
        self._actualizar_audio()

    def _programar_poll_audio(self):
        self._poll_audio_id = self.root.after(100, self._poll_audio)

    def _poll_audio(self):
        self._poll_audio_id = None
        self._actualizar_audio()
        if self._cerrando and self.control_prueba.estado().fase == "closed":
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

    def _cancelar(self):
        if self._cerrando:
            return
        self._cerrando = True
        self.boton_cancelar.state(["disabled"])
        self.control_prueba.cerrar()
        self._actualizar_audio()


def main() -> int:
    try:
        base = cargar_configuracion_actual()
        snapshot = snapshot_configuracion(base)
        capacidades = obtener_capacidades()
        inventario = cargar_inventario_entradas()
    except Exception as exc:
        print(f"[settings] no se pudo cargar la configuración: {exc}",
              file=sys.stderr)
        return 2

    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception as exc:
        print(f"[settings] no se pudo abrir la ventana: {exc}",
              file=sys.stderr)
        return 1

    VentanaSettings(
        root,
        ControlSettings(base, snapshot),
        capacidades,
        inventario=inventario.dispositivos,
        error_inventario=inventario.error,
    )
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
