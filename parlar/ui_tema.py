"""Sistema visual de ParlAR para Tk/ttk: tokens, tema y controles propios.

Carbón cálido con acento ladrillo: sobrio, denso y nativo. Todo color,
espacio y tamaño de letra sale de acá; la vista no hardcodea valores.

- Los tamaños de letra van en puntos (Tk los escala con el DPI).
- Los espacios y medidas en píxeles pasan por ``Tema.px``, que escala con
  el DPI real de la pantalla (100 % = 96 dpi).
- Sin timers permanentes: la única animación (el toggle) corre sólo
  mientras se mueve.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Paleta:
    fondo: str = "#18191B"            # ventana: carbón, no negro
    lateral: str = "#121315"          # sidebar y header: neutro más hondo
    superficie: str = "#202225"       # grupos y controles elevados
    superficie_hover: str = "#282A2E"
    boton: str = "#2C2F33"            # secundario: visible sobre superficie
    boton_hover: str = "#35383D"
    campo: str = "#141517"            # interior de campos de texto
    borde: str = "#2C2F33"
    borde_fuerte: str = "#3B3F44"
    texto: str = "#E9E6E1"            # alto contraste, no blanco puro
    texto_secundario: str = "#9C9EA2"
    texto_deshabilitado: str = "#6A6D72"
    acento: str = "#A9523A"           # ladrillo / terracota apagado
    acento_hover: str = "#9C4A33"
    acento_presionado: str = "#86402C"
    acento_texto: str = "#E08A66"     # acento legible sobre carbón
    acento_suave: str = "#3A2620"     # selección y fondos activos
    sobre_acento: str = "#FFF4EC"
    exito: str = "#7DB88A"
    aviso: str = "#D9A84E"
    error: str = "#E07A6E"
    perilla: str = "#F2EEE9"
    perilla_apagada: str = "#A7A9AD"


PALETA = Paleta()

# Escala de espacio en px a 100 % (se escalan con Tema.px).
ESPACIO = {"xs": 4, "s": 8, "m": 12, "l": 16, "xl": 24}

# Escala tipográfica (puntos) y pesos: pocos pasos, una sola familia.
TIPOS = {
    "titulo": (15, "bold"),
    "seccion": (11, "bold"),
    "etiqueta": (10, "normal"),
    "cuerpo": (10, "normal"),
    "secundario": (9, "normal"),
    "nav": (10, "normal"),
    "nav_activa": (10, "bold"),
    "teclas": (10, "bold"),
}

# Tono de estado -> color de texto/indicador.
TONOS = {
    "ready": PALETA.exito,
    "paused": PALETA.aviso,
    "starting": PALETA.aviso,
    "attention": PALETA.aviso,
    "unavailable": PALETA.error,
    "stopped": PALETA.texto_secundario,
}


class Tema:
    """Aplica el tema ttk a una raíz y expone tokens escalados."""

    def __init__(self, root):
        import tkinter.font as tkfont
        from tkinter import ttk

        self.root = root
        self.ttk = ttk
        try:
            dpi = float(root.winfo_fpixels("1i"))
        except Exception:
            dpi = 96.0
        self.escala = max(1.0, dpi / 96.0)
        base = tkfont.nametofont("TkDefaultFont", root=root).actual()
        self.familia = base.get("family") or "TkDefaultFont"
        self.fuentes = {}
        for nombre, (tamano, peso) in TIPOS.items():
            self.fuentes[nombre] = tkfont.Font(
                root=root, family=self.familia, size=tamano, weight=peso)
        self._aplicar()

    def px(self, valor: float) -> int:
        return max(1, round(valor * self.escala))

    def espacio(self, nombre: str) -> int:
        return self.px(ESPACIO[nombre])

    # ------------------------------------------------------------------
    def _aplicar(self):
        p = PALETA
        ttk = self.ttk
        root = self.root
        estilo = ttk.Style(root)
        if "clam" in estilo.theme_names():
            estilo.theme_use("clam")
        f = self.fuentes
        root.configure(background=p.fondo)

        # Widgets clásicos (Text, listas desplegables de Combobox, modales).
        root.option_add("*Toplevel.background", p.fondo)
        root.option_add("*TCombobox*Listbox.background", p.superficie)
        root.option_add("*TCombobox*Listbox.foreground", p.texto)
        root.option_add("*TCombobox*Listbox.selectBackground", p.acento)
        root.option_add("*TCombobox*Listbox.selectForeground", p.sobre_acento)
        root.option_add("*TCombobox*Listbox.borderWidth", 0)
        root.option_add("*TCombobox*Listbox.font", f["cuerpo"])

        estilo.configure(
            ".", background=p.fondo, foreground=p.texto,
            fieldbackground=p.campo, bordercolor=p.borde,
            lightcolor=p.superficie, darkcolor=p.superficie,
            troughcolor=p.lateral, selectbackground=p.acento,
            selectforeground=p.sobre_acento, insertcolor=p.acento_texto,
            focuscolor=p.acento_texto, font=f["cuerpo"])
        estilo.map(".", foreground=[("disabled", p.texto_deshabilitado)])

        # Contenedores.
        estilo.configure("TFrame", background=p.fondo)
        estilo.configure("Lateral.TFrame", background=p.lateral)
        estilo.configure("Header.TFrame", background=p.lateral)
        estilo.configure("Footer.TFrame", background=p.lateral)
        estilo.configure("Grupo.TFrame", background=p.superficie,
                         bordercolor=p.borde, relief="flat")
        estilo.configure("Linea.TFrame", background=p.borde)

        # Texto.
        estilo.configure("TLabel", background=p.fondo, foreground=p.texto,
                         font=f["cuerpo"])
        estilo.configure("Titulo.TLabel", background=p.lateral,
                         foreground=p.texto, font=f["titulo"])
        estilo.configure("Section.TLabel", background=p.fondo,
                         foreground=p.texto, font=f["seccion"])
        estilo.configure("Grupo.TLabel", background=p.fondo,
                         foreground=p.texto_secundario,
                         font=self.fuentes["nav_activa"])
        estilo.configure("Etiqueta.TLabel", background=p.superficie,
                         foreground=p.texto, font=f["etiqueta"])
        estilo.configure("Ayuda.TLabel", background=p.superficie,
                         foreground=p.texto_secundario, font=f["secundario"])
        estilo.configure("Status.TLabel", background=p.fondo,
                         foreground=p.texto_secundario, font=f["secundario"])
        for tono, color in (("Success", p.exito), ("Warning", p.aviso),
                            ("Error", p.error)):
            estilo.configure(f"{tono}.Status.TLabel", background=p.fondo,
                             foreground=color, font=f["secundario"])
        # Variantes sobre header/footer (fondo lateral).
        estilo.configure("Header.Status.TLabel", background=p.lateral,
                         foreground=p.texto_secundario, font=f["secundario"])
        estilo.configure("Footer.Status.TLabel", background=p.lateral,
                         foreground=p.texto_secundario, font=f["secundario"])
        for tono, color in (("Success", p.exito), ("Warning", p.aviso),
                            ("Error", p.error)):
            estilo.configure(f"{tono}.Header.Status.TLabel",
                             background=p.lateral, foreground=color,
                             font=f["secundario"])
            estilo.configure(f"{tono}.Footer.Status.TLabel",
                             background=p.lateral, foreground=color,
                             font=f["secundario"])
        estilo.configure("Estado.TLabel", background=p.lateral,
                         foreground=p.texto, font=f["seccion"])
        estilo.configure("Teclas.TLabel", background=p.campo,
                         foreground=p.texto, font=f["teclas"],
                         padding=(self.px(10), self.px(5)))

        # Botones: primario (ladrillo), secundario (superficie), navegación.
        relleno = (self.px(14), self.px(6))
        estilo.configure(
            "TButton", background=p.boton, foreground=p.texto,
            bordercolor=p.borde_fuerte, lightcolor=p.boton,
            darkcolor=p.boton, focuscolor=p.acento_texto,
            focusthickness=1, padding=relleno, relief="flat",
            font=f["cuerpo"])
        estilo.map(
            "TButton",
            background=[("disabled", p.superficie), ("pressed", p.borde),
                        ("active", p.boton_hover)],
            bordercolor=[("disabled", p.borde), ("focus", p.acento_texto)],
            lightcolor=[("disabled", p.superficie), ("pressed", p.borde),
                        ("active", p.boton_hover)],
            darkcolor=[("disabled", p.superficie), ("pressed", p.borde),
                       ("active", p.boton_hover)],
            foreground=[("disabled", p.texto_deshabilitado)])
        estilo.configure(
            "Primario.TButton", background=p.acento, foreground=p.sobre_acento,
            bordercolor=p.acento, lightcolor=p.acento, darkcolor=p.acento,
            font=f["nav_activa"])
        estilo.map(
            "Primario.TButton",
            background=[("disabled", p.superficie),
                        ("pressed", p.acento_presionado),
                        ("active", p.acento_hover)],
            bordercolor=[("disabled", p.borde), ("focus", p.perilla)],
            lightcolor=[("disabled", p.superficie),
                        ("pressed", p.acento_presionado),
                        ("active", p.acento_hover)],
            darkcolor=[("disabled", p.superficie),
                       ("pressed", p.acento_presionado),
                       ("active", p.acento_hover)],
            foreground=[("disabled", p.texto_deshabilitado)])
        estilo.configure(
            "Peligro.TButton", foreground=p.error, bordercolor=p.borde_fuerte)
        estilo.map(
            "Peligro.TButton",
            foreground=[("disabled", p.texto_deshabilitado)],
            bordercolor=[("focus", p.error)])
        estilo.configure(
            "Nav.TButton", background=p.lateral, foreground=p.texto_secundario,
            bordercolor=p.lateral, lightcolor=p.lateral, darkcolor=p.lateral,
            anchor="w", padding=(self.px(12), self.px(7)), font=f["nav"],
            focusthickness=1, focuscolor=p.acento_texto)
        estilo.map(
            "Nav.TButton",
            background=[("selected", p.superficie),
                        ("active", p.superficie_hover)],
            lightcolor=[("selected", p.superficie),
                        ("active", p.superficie_hover)],
            darkcolor=[("selected", p.superficie),
                       ("active", p.superficie_hover)],
            bordercolor=[("focus", p.acento_texto),
                         ("selected", p.superficie)],
            foreground=[("selected", p.texto), ("active", p.texto)],
            font=[("selected", f["nav_activa"])])

        # Campos.
        campo = dict(fieldbackground=p.campo, foreground=p.texto,
                     bordercolor=p.borde_fuerte, lightcolor=p.campo,
                     darkcolor=p.campo, insertcolor=p.acento_texto,
                     padding=(self.px(8), self.px(5)))
        estilo.configure("TEntry", **campo)
        estilo.map("TEntry",
                   bordercolor=[("focus", p.acento_texto),
                                ("hover", p.texto_deshabilitado)],
                   fieldbackground=[("readonly", p.campo),
                                    ("disabled", p.fondo)])
        estilo.configure("TCombobox", **campo, arrowcolor=p.texto_secundario,
                         background=p.superficie,
                         selectbackground=p.campo,
                         selectforeground=p.texto)
        estilo.map("TCombobox",
                   bordercolor=[("focus", p.acento_texto),
                                ("hover", p.texto_deshabilitado)],
                   fieldbackground=[("disabled", p.fondo),
                                    ("readonly", p.campo)],
                   foreground=[("disabled", p.texto_deshabilitado)],
                   background=[("active", p.superficie_hover)],
                   arrowcolor=[("disabled", p.texto_deshabilitado),
                               ("active", p.texto)],
                   selectbackground=[("focus", p.campo)],
                   selectforeground=[("focus", p.texto)])

        # Medidor del micrófono: fino, ladrillo.
        estilo.configure("Medidor.Horizontal.TProgressbar",
                         troughcolor=p.campo, background=p.acento,
                         bordercolor=p.borde, lightcolor=p.acento,
                         darkcolor=p.acento, thickness=self.px(6))

        # Scrollbar fina sin flechas.
        estilo.layout("Fina.Vertical.TScrollbar", [
            ("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
                ("Vertical.Scrollbar.thumb",
                 {"expand": "1", "sticky": "nswe"})]})])
        estilo.configure("Fina.Vertical.TScrollbar", troughcolor=p.fondo,
                         background=p.borde_fuerte, bordercolor=p.fondo,
                         lightcolor=p.borde_fuerte, darkcolor=p.borde_fuerte,
                         gripcount=0, arrowsize=self.px(8))
        estilo.map("Fina.Vertical.TScrollbar",
                   background=[("active", p.texto_deshabilitado)])
        estilo.configure("TSeparator", background=p.borde)

    def estilo_texto_libre(self):
        """Opciones para tk.Text con el mismo lenguaje que los campos."""
        p = PALETA
        return dict(
            background=p.campo, foreground=p.texto,
            insertbackground=p.acento_texto, selectbackground=p.acento,
            selectforeground=p.sobre_acento, relief="flat", borderwidth=0,
            highlightthickness=1, highlightbackground=p.borde_fuerte,
            highlightcolor=p.acento_texto, font=self.fuentes["cuerpo"],
            padx=self.px(8), pady=self.px(6))


class Interruptor:
    """Toggle accesible: fila con título, ayuda opcional y switch.

    - Click en cualquier parte de la fila, Space o Return cambian el valor.
    - Foco visible (anillo), hover, y estado deshabilitado legible.
    - ON/OFF se distingue por color Y por la posición de la perilla.
    - Imita lo mínimo de ttk.Checkbutton que usa la vista: ``state``,
      ``instate``, ``cget("text")``, ``configure(text=)`` y ``focus_set``.
    """

    ANCHO, ALTO = 36, 20
    DURACION_MS = 120
    PASOS = 6

    def __init__(self, padre, tema: Tema, *, text: str, variable,
                 ayuda: str | None = None, command=None,
                 estilo_fondo: str | None = None):
        import tkinter as tk

        self.tema = tema
        self.variable = variable
        self.command = command
        self._texto = text
        self._deshabilitado = False
        self._hover = False
        self._foco = False
        self._posicion = 1.0 if variable.get() else 0.0
        self._anim = None
        p = PALETA
        fondo = estilo_fondo or p.superficie
        self._fondo = fondo
        ttk = tema.ttk

        self.fila = tk.Frame(padre, background=fondo, highlightthickness=0,
                             cursor="hand2")
        self.fila.columnconfigure(0, weight=1)
        self.etiqueta = tk.Label(
            self.fila, text=text, background=fondo, foreground=p.texto,
            font=tema.fuentes["etiqueta"], anchor="w", justify="left")
        self.etiqueta.grid(row=0, column=0, sticky="w")
        self.ayuda = None
        if ayuda:
            self.ayuda = tk.Label(
                self.fila, text=ayuda, background=fondo,
                foreground=p.texto_secundario,
                font=tema.fuentes["secundario"], anchor="w", justify="left",
                wraplength=tema.px(380))
            self.ayuda.grid(row=1, column=0, sticky="w",
                            pady=(tema.px(2), 0))
        ancho, alto = tema.px(self.ANCHO + 6), tema.px(self.ALTO + 6)
        self.lienzo = tk.Canvas(
            self.fila, width=ancho, height=alto, background=fondo,
            highlightthickness=0, borderwidth=0, takefocus=1,
            cursor="hand2")
        self.lienzo.grid(row=0, column=1, rowspan=2, sticky="e",
                         padx=(tema.px(12), 0))
        del ttk

        for widget in self._superficies():
            widget.bind("<Button-1>", self._click, add="+")
            widget.bind("<Enter>", lambda _e: self._set_hover(True), add="+")
            widget.bind("<Leave>", lambda _e: self._set_hover(False), add="+")
        self.lienzo.bind("<space>", self._tecla)
        self.lienzo.bind("<Return>", self._tecla)
        self.lienzo.bind("<FocusIn>", lambda _e: self._set_foco(True))
        self.lienzo.bind("<FocusOut>", lambda _e: self._set_foco(False))
        self._traza = variable.trace_add("write", self._al_cambiar_variable)
        self.lienzo.bind("<Destroy>", self._al_destruir, add="+")
        self._dibujar()

    # ------------------------------------------------- API tipo ttk
    def grid(self, **kwargs):
        self.fila.grid(**kwargs)

    def bind(self, secuencia, funcion, add=None):
        return self.lienzo.bind(secuencia, funcion, add)

    def focus_set(self):
        self.lienzo.focus_set()

    def winfo_class(self):
        return "Interruptor"

    def __getattr__(self, nombre):
        # winfo_*, after, etc. se delegan al lienzo (el widget enfocable).
        if nombre.startswith("_") or "lienzo" not in self.__dict__:
            raise AttributeError(nombre)
        return getattr(self.lienzo, nombre)

    def cget(self, opcion):
        if opcion == "text":
            return self._texto
        return self.lienzo.cget(opcion)

    def configure(self, **kwargs):
        if "text" in kwargs:
            self._texto = kwargs.pop("text")
            self.etiqueta.configure(text=self._texto)
        if kwargs:
            self.lienzo.configure(**kwargs)

    config = configure

    def state(self, especificacion=None):
        if especificacion is None:
            return ("disabled",) if self._deshabilitado else ()
        for marca in especificacion:
            if marca == "disabled":
                self._deshabilitado = True
            elif marca == "!disabled":
                self._deshabilitado = False
        self.lienzo.configure(takefocus=0 if self._deshabilitado else 1)
        cursor = "" if self._deshabilitado else "hand2"
        for widget in self._superficies():
            widget.configure(cursor=cursor)
        p = PALETA
        color = p.texto_deshabilitado if self._deshabilitado else p.texto
        self.etiqueta.configure(foreground=color)
        if self.ayuda is not None:
            self.ayuda.configure(
                foreground=p.texto_deshabilitado if self._deshabilitado
                else p.texto_secundario)
        self._dibujar()
        return ()

    def instate(self, especificacion):
        for marca in especificacion:
            if marca == "disabled" and not self._deshabilitado:
                return False
            if marca == "!disabled" and self._deshabilitado:
                return False
        return True

    # ------------------------------------------------- interacción
    def _superficies(self):
        widgets = [self.fila, self.etiqueta, self.lienzo]
        if self.ayuda is not None:
            widgets.append(self.ayuda)
        return widgets

    def alternar(self):
        if self._deshabilitado:
            return
        self.variable.set(not bool(self.variable.get()))
        if self.command is not None:
            self.command()

    def _click(self, _evento=None):
        if self._deshabilitado:
            return "break"
        self.lienzo.focus_set()
        self.alternar()
        return "break"

    def _tecla(self, _evento=None):
        self.alternar()
        return "break"

    def _set_hover(self, valor):
        self._hover = valor
        self._dibujar()

    def _set_foco(self, valor):
        self._foco = valor
        self._dibujar()

    def _al_destruir(self, _evento=None):
        if self._anim is not None:
            try:
                self.lienzo.after_cancel(self._anim)
            except Exception:
                pass
            self._anim = None
        try:
            self.variable.trace_remove("write", self._traza)
        except Exception:
            pass

    def _al_cambiar_variable(self, *_args):
        destino = 1.0 if self.variable.get() else 0.0
        if self._anim is not None:
            try:
                self.lienzo.after_cancel(self._anim)
            except Exception:
                pass
            self._anim = None
        try:
            visible = bool(self.lienzo.winfo_ismapped())
        except Exception:
            visible = False
        if not visible:
            self._posicion = destino
            self._dibujar()
            return
        inicio = self._posicion
        paso_ms = max(1, self.DURACION_MS // self.PASOS)

        def avanzar(n=1):
            t = n / self.PASOS
            facil = 1 - (1 - t) ** 3            # ease-out, sin rebote
            self._posicion = inicio + (destino - inicio) * facil
            self._dibujar()
            if n < self.PASOS:
                self._anim = self.lienzo.after(paso_ms, avanzar, n + 1)
            else:
                self._anim = None

        avanzar()

    @property
    def animando(self) -> bool:
        return self._anim is not None

    # ------------------------------------------------- dibujo
    def _dibujar(self):
        p = PALETA
        t = self.tema
        c = self.lienzo
        c.delete("all")
        margen = t.px(3)
        ancho, alto = t.px(self.ANCHO), t.px(self.ALTO)
        x0, y0 = margen, margen
        x1, y1 = x0 + ancho, y0 + alto
        encendido = self._posicion >= 0.5
        if self._deshabilitado:
            pista = p.acento_suave if encendido else p.superficie_hover
            perilla = p.texto_deshabilitado
            borde = p.borde_fuerte
        elif encendido:
            pista = p.acento_hover if self._hover else p.acento
            perilla = p.perilla
            borde = pista
        else:
            pista = p.superficie_hover if self._hover else p.campo
            perilla = p.perilla_apagada
            borde = p.texto_secundario if self._hover else p.texto_deshabilitado
        radio = alto / 2
        self._capsula(c, x0, y0, x1, y1, radio, pista, borde)
        if self._foco and not self._deshabilitado:
            self._capsula(c, x0 - t.px(2), y0 - t.px(2), x1 + t.px(2),
                          y1 + t.px(2), radio + t.px(2), "", p.acento_texto,
                          ancho_linea=max(1, t.px(1)))
        diametro = alto - t.px(6)
        recorrido = ancho - t.px(6) - diametro
        cx = x0 + t.px(3) + recorrido * self._posicion
        cy = y0 + t.px(3)
        c.create_oval(cx, cy, cx + diametro, cy + diametro,
                      fill=perilla, outline="")

    @staticmethod
    def _capsula(c, x0, y0, x1, y1, r, relleno, borde, ancho_linea=1):
        if relleno:
            c.create_oval(x0, y0, x0 + 2 * r, y1, fill=relleno, outline="")
            c.create_oval(x1 - 2 * r, y0, x1, y1, fill=relleno, outline="")
            c.create_rectangle(x0 + r, y0, x1 - r, y1, fill=relleno,
                               outline="")
        if borde:
            c.create_arc(x0, y0, x0 + 2 * r, y1, start=90, extent=180,
                         style="arc", outline=borde, width=ancho_linea)
            c.create_arc(x1 - 2 * r, y0, x1, y1, start=-90, extent=180,
                         style="arc", outline=borde, width=ancho_linea)
            c.create_line(x0 + r, y0, x1 - r, y0, fill=borde,
                          width=ancho_linea)
            c.create_line(x0 + r, y1, x1 - r, y1, fill=borde,
                          width=ancho_linea)


class IndicadorEstado:
    """Punto de estado: relleno = activo, anillo = inactivo (no sólo color)."""

    def __init__(self, padre, tema: Tema, fondo: str):
        import tkinter as tk

        self.tema = tema
        d = tema.px(10)
        self.lienzo = tk.Canvas(padre, width=d + 2, height=d + 2,
                                background=fondo, highlightthickness=0,
                                borderwidth=0, takefocus=0)
        self._d = d
        self.fijar("stopped")

    def grid(self, **kwargs):
        self.lienzo.grid(**kwargs)

    def fijar(self, categoria: str, *, lleno: bool | None = None):
        color = TONOS.get(categoria, PALETA.texto_secundario)
        if lleno is None:
            lleno = categoria in {"ready", "attention", "paused", "starting"}
        self.categoria = categoria
        c = self.lienzo
        c.delete("all")
        d = self._d
        if lleno:
            c.create_oval(1, 1, d, d, fill=color, outline=color)
        else:
            ancho = max(1, self.tema.px(2))
            c.create_oval(1 + ancho / 2, 1 + ancho / 2, d - ancho / 2,
                          d - ancho / 2, outline=color, width=ancho)
