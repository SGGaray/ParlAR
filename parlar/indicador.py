"""Indicador mínimo: un puntito sin bordes, siempre visible.

gris    = inactivo
rojo    = grabando
ámbar   = transcribiendo

tkinter corre en el hilo PRINCIPAL (requisito de tk); los hilos de trabajo
empujan cambios de estado a través de una variable protegida, sondeada con
after(). Si no hay display o el indicador está deshabilitado, BucleSinUI
mantiene vivo el proceso.

Nota: las claves de estado ("idle"/"recording"/"transcribing") son protocolo
interno compartido con app.py; se mantienen en inglés a propósito.
"""

import threading
import time

COLORES = {"idle": "#6b7280", "recording": "#dc2626", "transcribing": "#f59e0b"}


class Indicador:
    def __init__(self, al_click=None):
        import tkinter as tk
        self.tk = tk
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", 0.85)
        except Exception:
            pass
        tam = 26
        sw = self.root.winfo_screenwidth()
        self.root.geometry(f"{tam}x{tam}+{sw - tam - 16}+16")
        self.canvas = tk.Canvas(self.root, width=tam, height=tam,
                                highlightthickness=0, bg="#111111")
        self.canvas.pack()
        self.punto = self.canvas.create_oval(5, 5, tam - 5, tam - 5,
                                             fill=COLORES["idle"], outline="")
        self._estado = "idle"
        self._lock = threading.Lock()
        self._salir = False
        if al_click:
            self.canvas.bind("<Button-1>", lambda e: al_click())
        # permite arrastrar el punto por la pantalla
        self.canvas.bind("<Button-3>", self._arrastre_inicio)
        self.canvas.bind("<B3-Motion>", self._arrastre_mover)
        self.root.after(100, self._sondear)

    def _arrastre_inicio(self, e):
        self._dx, self._dy = e.x, e.y

    def _arrastre_mover(self, e):
        x = self.root.winfo_pointerx() - self._dx
        y = self.root.winfo_pointery() - self._dy
        self.root.geometry(f"+{x}+{y}")

    def fijar_estado(self, estado: str):
        with self._lock:
            self._estado = estado

    def cerrar(self):
        self._salir = True

    def _sondear(self):
        if self._salir:
            self.root.destroy()
            return
        with self._lock:
            estado = self._estado
        self.canvas.itemconfig(self.punto, fill=COLORES.get(estado, COLORES["idle"]))
        self.root.after(120, self._sondear)

    def ejecutar(self):
        self.root.mainloop()


class BucleSinUI:
    """Mantiene vivo el hilo principal cuando el indicador no está disponible."""

    def __init__(self):
        self._salir = False

    def fijar_estado(self, estado: str):
        pass

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
