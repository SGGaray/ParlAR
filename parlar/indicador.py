"""Indicador mínimo: waveform sin bordes, visible sólo durante el dictado.

gris  = inactivo
rojo  = grabando
ámbar = transcribiendo/deteniendo
rosa  = error operativo

tkinter corre en el hilo PRINCIPAL (requisito de tk); los hilos de trabajo
empujan cambios de estado a través de una variable protegida, sondeada con
after(). Si no hay display o el indicador está deshabilitado, BucleSinUI
mantiene vivo el proceso.

Nota: las claves de estado ("idle"/"recording"/"transcribing") son protocolo
interno compartido con app.py; se mantienen en inglés a propósito.
"""

import time
from dataclasses import dataclass

from .estado_ui import EstadoInterfaz

ANCHO_INDICADOR = 70
ALTO_INDICADOR = 28
MARGEN_INFERIOR = 12
INTERVALO_ANIMACION_MS = 90
FONDO = "#15171a"

COLORES = {
    "idle": "#9ca3af",
    "recording": "#ef4444",
    "transcribing": "#f59e0b",
    "error": "#e11d48",
}

_PATRONES = {
    "idle": (
        (4, 6, 8, 10, 8, 6, 4),
    ),
    "recording": (
        (4, 8, 12, 18, 12, 8, 4),
        (6, 12, 16, 10, 16, 12, 6),
        (10, 16, 8, 14, 8, 16, 10),
        (12, 8, 14, 18, 14, 8, 12),
        (8, 14, 18, 10, 18, 14, 8),
        (6, 10, 14, 16, 14, 10, 6),
    ),
    "transcribing": (
        (4, 6, 8, 10, 8, 6, 4),
        (4, 7, 9, 12, 9, 7, 4),
        (5, 8, 10, 12, 10, 8, 5),
        (4, 7, 9, 11, 9, 7, 4),
    ),
    "error": (
        (6, 10, 14, 18, 14, 10, 6),
    ),
}


@dataclass(frozen=True, slots=True)
class FrameWaveform:
    alturas: tuple[int, ...]
    color: str


def calcular_frame_waveform(operativo: str, fase: int) -> FrameWaveform:
    """Devuelve un frame visual puro; no consulta ni modifica EstadoUI."""
    estado = operativo if operativo in _PATRONES else "idle"
    patrones = _PATRONES[estado]
    alturas = patrones[fase % len(patrones)]
    return FrameWaveform(alturas=alturas, color=COLORES[estado])


def calcular_posicion(ancho_pantalla: int, alto_pantalla: int) -> tuple[int, int]:
    """Centra el indicador y conserva un margen corto sobre el borde inferior."""
    x = max(0, (ancho_pantalla - ANCHO_INDICADOR) // 2)
    y = max(0, alto_pantalla - ALTO_INDICADOR - MARGEN_INFERIOR)
    return x, y


class Indicador:
    def __init__(self, al_click=None):
        import tkinter as tk
        self.tk = tk
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", 0.92)
        except Exception:
            pass
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x, y = calcular_posicion(sw, sh)
        self.root.geometry(
            f"{ANCHO_INDICADOR}x{ALTO_INDICADOR}+{x}+{y}")
        self.canvas = tk.Canvas(
            self.root,
            width=ANCHO_INDICADOR,
            height=ALTO_INDICADOR,
            highlightthickness=0,
            borderwidth=0,
            bg=FONDO,
        )
        self.canvas.pack()
        centro_y = ALTO_INDICADOR // 2
        self._barras = [
            self.canvas.create_line(
                11 + indice * 8,
                centro_y - 2,
                11 + indice * 8,
                centro_y + 2,
                fill=COLORES["idle"],
                width=4,
                capstyle=tk.ROUND,
            )
            for indice in range(7)
        ]
        self._fase = 0
        self._estado_ui = EstadoInterfaz()
        self._ventana_visible = False
        self.root.withdraw()
        self._salir = False
        if al_click:
            self.canvas.bind("<Button-1>", lambda e: al_click())
        # Permite apartar temporalmente la waveform si cubre contenido.
        self.canvas.bind("<Button-3>", self._arrastre_inicio)
        self.canvas.bind("<B3-Motion>", self._arrastre_mover)
        self.root.after(INTERVALO_ANIMACION_MS, self._sondear)

    def _arrastre_inicio(self, e):
        self._dx, self._dy = e.x, e.y

    def _arrastre_mover(self, e):
        x = self.root.winfo_pointerx() - self._dx
        y = self.root.winfo_pointery() - self._dy
        self.root.geometry(f"+{x}+{y}")

    def fijar_estado(self, estado: str):
        self._estado_ui.fijar_operativo(estado)

    def fijar_continuo(self, activo: bool):
        self._estado_ui.fijar_continuo(activo)

    def fijar_presionado(self, activo: bool):
        self._estado_ui.fijar_presionado(activo)

    def snapshot_ui(self):
        return self._estado_ui.snapshot()

    def cerrar(self):
        self._salir = True

    def _renderizar(self, operativo: str):
        frame = calcular_frame_waveform(operativo, self._fase)
        for barra, altura in zip(self._barras, frame.alturas):
            x, _, _, _ = self.canvas.coords(barra)
            y_inicial = (ALTO_INDICADOR - altura) // 2
            self.canvas.coords(barra, x, y_inicial, x, y_inicial + altura)
            self.canvas.itemconfig(barra, fill=frame.color)
        self._fase += 1

    def _sondear(self):
        if self._salir:
            self.root.destroy()
            return
        snapshot = self._estado_ui.snapshot()
        if snapshot.visible:
            self._renderizar(snapshot.operativo)
            self.root.deiconify()
        else:
            self._fase = 0
            self.root.withdraw()
        self.root.after(INTERVALO_ANIMACION_MS, self._sondear)

    def ejecutar(self):
        self.root.mainloop()


class BucleSinUI:
    """Mantiene vivo el hilo principal cuando el indicador no está disponible."""

    def __init__(self):
        self._salir = False
        self._estado_ui = EstadoInterfaz()

    def fijar_estado(self, estado: str):
        self._estado_ui.fijar_operativo(estado)

    def fijar_continuo(self, activo: bool):
        self._estado_ui.fijar_continuo(activo)

    def fijar_presionado(self, activo: bool):
        self._estado_ui.fijar_presionado(activo)

    def snapshot_ui(self):
        return self._estado_ui.snapshot()

    def cerrar(self):
        self._salir = True

    def ejecutar(self):
        while not self._salir:
            time.sleep(0.2)


def crear_ui(habilitado: bool, al_click=None):
    if habilitado:
        try:
            return Indicador(al_click=al_click)
        except Exception as e:
            print(f"[indicador] no disponible ({e}); corriendo sin UI")
    return BucleSinUI()
