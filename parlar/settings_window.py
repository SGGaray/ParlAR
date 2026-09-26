"""Ventana Tkinter básica para editar Settings persistidos de ParlAR.

La lógica de formulario y persistencia permanece separada de Tk para que el
contrato pueda probarse sin display. Guardar nunca modifica una ``App`` activa:
los cambios se escriben para el próximo inicio mediante ``settings_backend``.
"""

import dataclasses
import sys
from collections.abc import Callable
from dataclasses import dataclass

from .settings_backend import (
    ResultadoPersistencia,
    SettingsCapabilities,
    SettingsSnapshot,
    cargar_configuracion_actual,
    construir_configuracion_candidata,
    obtener_capacidades,
    persistir_configuracion,
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
            capacidades: SettingsCapabilities):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.control = control
        self.capacidades = capacidades
        self._creando = True

        self.root.title("ParlAR Settings")
        self.root.geometry("680x720")
        self.root.minsize(680, 720)
        self.root.protocol("WM_DELETE_WINDOW", self._cancelar)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        self._configurar_estilos()
        self._crear_variables(
            valores_desde_snapshot(control.snapshot_inicial))
        self._crear_contenido()
        self._conectar_cambios()
        self._creando = False
        self._actualizar_sucio()

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
        }
        self.estado = tk.StringVar(value="")
        self._contexto_inicial = valores.context_terms

    def _crear_contenido(self):
        ttk = self.ttk
        contenedor = ttk.Frame(self.root, padding=16)
        contenedor.grid(row=0, column=0, sticky="nsew")
        contenedor.columnconfigure(0, weight=1)

        general = ttk.LabelFrame(contenedor, text="General", padding=10)
        general.grid(row=0, column=0, sticky="ew")
        for columna in (1, 3):
            general.columnconfigure(columna, weight=1)

        self._campo(general, "Modelo", "model_size", 0, 0)
        self._campo(
            general, "Dispositivo", "device", 0, 2,
            opciones=self.capacidades.devices)
        self._campo(
            general, "Tipo de cómputo", "compute_type", 1, 0,
            opciones=self.capacidades.compute_types)
        self._campo(general, "Idioma", "language", 1, 2)
        self._campo(
            general, "Modo", "mode", 2, 0,
            opciones=self.capacidades.modes)
        self._campo(
            general, "Reescritura", "rewrite_mode", 2, 2,
            opciones=self.capacidades.rewrite_modes)

        salida = ttk.LabelFrame(contenedor, text="Salida", padding=10)
        salida.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        salida.columnconfigure(1, weight=1)
        self._campo(
            salida, "Inyector", "injector", 0, 0,
            opciones=self.capacidades.injectors)
        ttk.Checkbutton(
            salida, text="Usar GuionAR",
            variable=self.variables["guionar"],
        ).grid(row=0, column=2, padx=(18, 0), sticky="w")
        ttk.Checkbutton(
            salida, text="Guardar sesión",
            variable=self.variables["guardar_sesion"],
        ).grid(row=0, column=3, padx=(18, 0), sticky="w")

        interfaz = ttk.LabelFrame(contenedor, text="Interfaz", padding=10)
        interfaz.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ttk.Checkbutton(
            interfaz, text="Mostrar overlay de dictado",
            variable=self.variables["overlay"],
        ).grid(row=0, column=0, sticky="w")

        atajo = ttk.LabelFrame(contenedor, text="Atajo", padding=10)
        atajo.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        atajo.columnconfigure(1, weight=1)
        ttk.Label(atajo, text="Combinación").grid(
            row=0, column=0, padx=(0, 12), sticky="w")
        ttk.Entry(
            atajo, textvariable=self.variables["hotkey_toggle"],
        ).grid(row=0, column=1, sticky="ew")
        ttk.Label(
            atajo,
            text="Edición textual; no captura teclas globales.",
            style="Status.TLabel",
        ).grid(row=1, column=1, pady=(6, 0), sticky="w")

        contexto = ttk.LabelFrame(contenedor, text="Contexto", padding=10)
        contexto.grid(row=4, column=0, sticky="nsew", pady=(10, 0))
        contexto.columnconfigure(0, weight=1)
        ttk.Label(
            contexto,
            text="Un término por línea. Las líneas vacías se ignoran.",
            style="Status.TLabel",
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
        socket.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        socket.columnconfigure(1, weight=1)
        ttk.Label(socket, text="Ruta").grid(
            row=0, column=0, padx=(0, 12), sticky="w")
        ttk.Entry(
            socket, textvariable=self.variables["guionar_socket"],
        ).grid(row=0, column=1, sticky="ew")

        pie = ttk.Frame(contenedor)
        pie.grid(row=6, column=0, sticky="ew", pady=(14, 0))
        pie.columnconfigure(0, weight=1)
        self.etiqueta_estado = ttk.Label(
            pie,
            textvariable=self.estado,
            style="Status.TLabel",
            wraplength=400,
        )
        self.etiqueta_estado.grid(row=0, column=0, sticky="w")
        ttk.Button(
            pie, text="Cancelar", command=self._cancelar,
        ).grid(row=0, column=1, padx=(12, 8))
        self.boton_guardar = ttk.Button(
            pie, text="Guardar", command=self._guardar)
        self.boton_guardar.grid(row=0, column=2)

    def _campo(
            self, padre, etiqueta, nombre, fila, columna, *, opciones=None):
        self.ttk.Label(padre, text=etiqueta).grid(
            row=fila, column=columna, padx=(0, 8), pady=5, sticky="w")
        if opciones is None:
            widget = self.ttk.Entry(
                padre, textvariable=self.variables[nombre])
        else:
            widget = self.ttk.Combobox(
                padre,
                textvariable=self.variables[nombre],
                values=opciones,
                state="readonly",
            )
        widget.grid(
            row=fila, column=columna + 1,
            padx=(0, 14), pady=5, sticky="ew")

    def _conectar_cambios(self):
        for variable in self.variables.values():
            variable.trace_add("write", self._al_cambiar)
        self.contexto.bind("<<Modified>>", self._al_modificar_contexto)

    def _al_cambiar(self, *_args):
        self._actualizar_sucio()

    def _al_modificar_contexto(self, _evento):
        if self.contexto.edit_modified():
            self.contexto.edit_modified(False)
            self._actualizar_sucio()

    def _valores(self) -> ValoresFormulario:
        return ValoresFormulario(
            model_size=self.variables["model_size"].get(),
            device=self.variables["device"].get(),
            compute_type=self.variables["compute_type"].get(),
            language=self.variables["language"].get(),
            context_terms=self.contexto.get("1.0", "end-1c"),
            mode=self.variables["mode"].get(),
            rewrite_mode=self.variables["rewrite_mode"].get(),
            injector=self.variables["injector"].get(),
            hotkey_toggle=self.variables["hotkey_toggle"].get(),
            overlay=self.variables["overlay"].get(),
            guionar=self.variables["guionar"].get(),
            guionar_socket=self.variables["guionar_socket"].get(),
            guardar_sesion=self.variables["guardar_sesion"].get(),
        )

    def _actualizar_sucio(self):
        if self._creando:
            return
        if self.control.esta_sucio(self._valores()):
            self.boton_guardar.state(["!disabled"])
        else:
            self.boton_guardar.state(["disabled"])

    def _guardar(self):
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
        self.control.cancelar(self.root.destroy)


def main() -> int:
    try:
        base = cargar_configuracion_actual()
        snapshot = snapshot_configuracion(base)
        capacidades = obtener_capacidades()
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

    VentanaSettings(root, ControlSettings(base, snapshot), capacidades)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
