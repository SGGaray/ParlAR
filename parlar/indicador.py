"""Indicador mínimo: waveform con contenedor sutil durante el dictado.

gris claro = procesando
blanco     = grabando
rosa       = error operativo

tkinter corre en el hilo PRINCIPAL (requisito de tk); los hilos de trabajo
empujan cambios de estado a través de una variable protegida, sondeada con
after(). Si no hay display o el indicador está deshabilitado, BucleSinUI
mantiene vivo el proceso.

Nota: las claves de estado ("idle"/"recording"/"transcribing") son protocolo
interno compartido con app.py; se mantienen en inglés a propósito.
"""

import math
import time
from dataclasses import dataclass

from .estado_ui import (
    CANTIDAD_BARRAS_VISUALES,
    ENVOLVENTE_REPOSO,
    EstadoInterfaz,
)

ANCHO_INDICADOR = 169
ALTO_INDICADOR = 44
MARGEN_INFERIOR = 22
INTERVALO_ANIMACION_MS = 33
INTERVALO_REPOSO_MS = 180
FONDO = "#111315"
BORDE = "#292c31"
CANTIDAD_BARRAS = CANTIDAD_BARRAS_VISUALES
ALTURA_REPOSO = 4
ALTURA_MAXIMA = 32
ANCHO_BARRA = 3
GAP_BARRAS = 5
PASO_BARRAS = ANCHO_BARRA + GAP_BARRAS
# 169 = 2×19 de padding + 17×3 de barra + 16×5 de gap real.
PADDING_HORIZONTAL = 19
OPACIDAD_MAXIMA = 0.94
FADE_MS = 132

# Estas constantes sólo gobiernan la presentación. No participan del VAD ni
# modifican el audio enviado a transcripción. El umbral de salida menor crea
# histéresis; ataque rápido y caída gradual evitan una waveform nerviosa.
PISO_RUIDO_ENTRADA = 0.018
PISO_RUIDO_SALIDA = 0.012
PICO_VISUAL_MINIMO = 0.022
MARGEN_PICO_VISUAL = 1.65
COEFICIENTE_PICO_SUBIDA = 0.45
COEFICIENTE_PICO_CAIDA = 0.006
COEFICIENTE_ATAQUE = 0.50
COEFICIENTE_CAIDA = 0.12
FORMA_LOCAL_MINIMA = 0.18
COEFICIENTE_FORMA_ATAQUE = 0.58
COEFICIENTE_FORMA_CAIDA = 0.20

COLORES = {
    "idle": "#777c84",
    "recording": "#f4f4f5",
    "transcribing": "#aeb4bd",
    "error": "#fb7185",
}

_ALTURAS_REPOSO = (ALTURA_REPOSO,) * CANTIDAD_BARRAS
_ALTURAS_ERROR = (
    4, 5, 7, 10, 14, 19, 25, 31, 25, 20, 15, 11, 8, 6, 5, 4, 4,
)


@dataclass(frozen=True, slots=True)
class FrameWaveform:
    alturas: tuple[int, ...]
    color: str


@dataclass(frozen=True, slots=True)
class NivelVisualSuavizado:
    nivel: float
    umbral_activo: bool
    pico_visual: float = PICO_VISUAL_MINIMO


def _limitar_nivel(nivel: float) -> float:
    try:
        valor = float(nivel)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(valor):
        return 0.0
    return min(1.0, max(0.0, valor))


def comprimir_nivel_visual(nivel: float) -> float:
    """Curva logarítmica moderada para una amplitud visual ya normalizada."""
    limitado = _limitar_nivel(nivel)
    return math.log1p(3.0 * limitado) / math.log(4.0)


def actualizar_pico_visual(
        nivel_post_piso: float,
        pico_anterior: float = PICO_VISUAL_MINIMO,
        activo: bool = True) -> float:
    """Peak follower visual: sube rápido y vuelve lentamente a su piso."""
    nivel = _limitar_nivel(nivel_post_piso) if activo else 0.0
    pico = min(1.0, max(PICO_VISUAL_MINIMO, _limitar_nivel(pico_anterior)))
    objetivo = max(PICO_VISUAL_MINIMO, nivel)
    coeficiente = (
        COEFICIENTE_PICO_SUBIDA
        if objetivo > pico
        else COEFICIENTE_PICO_CAIDA
    )
    actualizado = pico + coeficiente * (objetivo - pico)
    return min(1.0, max(PICO_VISUAL_MINIMO, actualizado))


def normalizar_nivel_adaptativo(
        nivel_post_piso: float, pico_visual: float) -> float:
    """Normaliza energía útil con headroom, sin amplificar el noise floor."""
    nivel = _limitar_nivel(nivel_post_piso)
    pico = min(1.0, max(PICO_VISUAL_MINIMO, _limitar_nivel(pico_visual)))
    return _limitar_nivel(nivel / (pico * MARGEN_PICO_VISUAL))


def calcular_envelope_espacial(
        cantidad: int = CANTIDAD_BARRAS) -> tuple[float, ...]:
    """Capacidad suave por barra, contenida en extremos y amplia al centro."""
    if cantidad <= 0:
        return ()
    if cantidad == 1:
        return (1.0,)
    centro = (cantidad - 1) / 2.0
    envelope = []
    for indice in range(cantidad):
        distancia = (indice - centro) / centro
        arco = math.cos(distancia * math.pi / 2.0)
        base = 0.26 + 0.74 * max(0.0, arco) ** 1.15
        # Dos frecuencias espaciales rompen la simetría geométrica sin perder
        # la jerarquía central ni introducir aleatoriedad por frame.
        asimetria = (
            1.0
            + 0.060 * math.sin(indice * 1.37 + 0.7)
            + 0.025 * math.cos(indice * 0.61 + 1.4)
        )
        envelope.append(_limitar_nivel(base * asimetria))
    return tuple(envelope)


def variacion_temporal(indice: int, fase: float) -> float:
    """Modulación continua y determinista que sólo altera la forma visual."""
    primaria = math.sin(fase * 5.0 + indice * 1.11)
    secundaria = math.cos(fase * 2.7 + indice * 0.69 + 0.8)
    return 1.0 + 0.105 * primaria + 0.055 * secundaria


def normalizar_forma_local(
        envolvente: tuple[float, ...]) -> tuple[float, ...]:
    """Normaliza la forma dentro del frame sin amplificar ruido diminuto."""
    if len(envolvente) != CANTIDAD_BARRAS:
        return ENVOLVENTE_REPOSO
    valores = tuple(_limitar_nivel(valor) for valor in envolvente)
    maximo = max(valores, default=0.0)
    if maximo <= 0.0:
        return ENVOLVENTE_REPOSO
    referencia = max(PISO_RUIDO_ENTRADA, maximo)
    recorrido = 1.0 - FORMA_LOCAL_MINIMA
    return tuple(
        FORMA_LOCAL_MINIMA
        + recorrido * (min(1.0, valor / referencia) ** 0.72)
        for valor in valores
    )


def suavizar_forma_barras(
        objetivo: tuple[float, ...],
        anterior: tuple[float, ...] = ENVOLVENTE_REPOSO,
        activo: bool = True) -> tuple[float, ...]:
    """Attack/release independiente para cada barra del descriptor visual."""
    if len(objetivo) != CANTIDAD_BARRAS:
        objetivo = ENVOLVENTE_REPOSO
    if len(anterior) != CANTIDAD_BARRAS:
        anterior = ENVOLVENTE_REPOSO
    destinos = (
        tuple(_limitar_nivel(valor) for valor in objetivo)
        if activo
        else ENVOLVENTE_REPOSO
    )
    suavizados = []
    for previo, destino in zip(anterior, destinos):
        previo = _limitar_nivel(previo)
        coeficiente = (
            COEFICIENTE_FORMA_ATAQUE
            if destino > previo
            else COEFICIENTE_FORMA_CAIDA
        )
        valor = previo + coeficiente * (destino - previo)
        if not activo and valor < 0.01:
            valor = 0.0
        suavizados.append(_limitar_nivel(valor))
    return tuple(suavizados)


def calcular_alturas_escucha(
        nivel_visual: float,
        fase: float,
        forma_local: tuple[float, ...] | None = None) -> tuple[int, ...]:
    """Convierte amplitud visual suavizada en una waveform orgánica."""
    amplitud = _limitar_nivel(nivel_visual)
    if amplitud == 0.0:
        return _ALTURAS_REPOSO
    recorrido = ALTURA_MAXIMA - ALTURA_REPOSO
    envelope = calcular_envelope_espacial()
    forma_audio = (
        None
        if forma_local is None or len(forma_local) != CANTIDAD_BARRAS
        else tuple(_limitar_nivel(valor) for valor in forma_local)
    )
    alturas = []
    for indice, capacidad in enumerate(envelope):
        if forma_audio is None:
            forma = capacidad * variacion_temporal(indice, fase)
        else:
            # El audio real gobierna el 92 % de la silueta. El envelope sólo
            # conserva un contorno mínimo para que el componente no se vuelva
            # un medidor técnico de columnas duras.
            forma = 0.92 * forma_audio[indice] + 0.08 * capacidad
        altura = ALTURA_REPOSO + round(recorrido * amplitud * forma)
        alturas.append(min(ALTURA_MAXIMA, max(ALTURA_REPOSO, altura)))
    return tuple(alturas)


def calcular_alturas_procesamiento(fase: float) -> tuple[int, ...]:
    """Tres pulsos neutros, reconocibles sin depender sólo del color."""
    alturas = [ALTURA_REPOSO] * CANTIDAD_BARRAS
    for orden, indice in enumerate((6, 8, 10)):
        onda = (math.sin(fase * 4.0 - orden * 1.35) + 1.0) / 2.0
        alturas[indice] = ALTURA_REPOSO + round(8 * onda ** 2)
    return tuple(alturas)


def calcular_intervalo_sondeo(
        visible: bool, transicion_visible: bool) -> int:
    """Anima a 30 FPS y reduce polling cuando el overlay está en reposo."""
    return (
        INTERVALO_ANIMACION_MS
        if visible or transicion_visible
        else INTERVALO_REPOSO_MS
    )


def interpolar_alturas(
        actuales: tuple[int, ...],
        objetivo: tuple[int, ...],
        factor: float) -> tuple[int, ...]:
    """Interpola siluetas sin dejar diferencias de un píxel estancadas."""
    proporcion = _limitar_nivel(factor)
    if len(actuales) != len(objetivo):
        return tuple(objetivo)
    resultado = []
    for actual, destino in zip(actuales, objetivo):
        diferencia = destino - actual
        if abs(diferencia) <= 1:
            resultado.append(destino)
        else:
            resultado.append(actual + round(diferencia * proporcion))
    return tuple(resultado)


def procesar_nivel_visual(
        nivel: float,
        nivel_anterior: float = 0.0,
        umbral_activo: bool = False,
        pico_anterior: float = PICO_VISUAL_MINIMO) -> NivelVisualSuavizado:
    """Aplica gain adaptativo, compresión y smoothing sólo a la UI."""
    entrada = _limitar_nivel(nivel)
    anterior = _limitar_nivel(nivel_anterior)
    if umbral_activo:
        activo = entrada > PISO_RUIDO_SALIDA
    else:
        activo = entrada >= PISO_RUIDO_ENTRADA

    nivel_post_piso = max(0.0, entrada - PISO_RUIDO_SALIDA) if activo else 0.0
    pico = actualizar_pico_visual(nivel_post_piso, pico_anterior, activo)
    normalizado = (
        normalizar_nivel_adaptativo(nivel_post_piso, pico)
        if activo
        else 0.0
    )
    objetivo = comprimir_nivel_visual(normalizado)
    coeficiente = (
        COEFICIENTE_ATAQUE
        if objetivo > anterior
        else COEFICIENTE_CAIDA
    )
    suavizado = anterior + coeficiente * (objetivo - anterior)
    if not activo and suavizado < 0.02:
        suavizado = 0.0
    return NivelVisualSuavizado(_limitar_nivel(suavizado), activo, pico)


def calcular_frame_waveform(
        operativo: str,
        fase: float,
        nivel_visual: float = 0.0,
        forma_local: tuple[float, ...] | None = None) -> FrameWaveform:
    """Devuelve un frame visual puro; no consulta ni modifica EstadoUI."""
    if operativo == "recording":
        alturas = calcular_alturas_escucha(
            nivel_visual, fase, forma_local)
    elif operativo == "transcribing":
        alturas = calcular_alturas_procesamiento(fase)
    elif operativo == "error":
        alturas = _ALTURAS_ERROR
    else:
        operativo = "idle"
        alturas = _ALTURAS_REPOSO
    return FrameWaveform(alturas=alturas, color=COLORES[operativo])


def calcular_posicion(
        ancho_pantalla: int,
        alto_pantalla: int,
        ancho_ventana: int = ANCHO_INDICADOR,
        alto_ventana: int = ALTO_INDICADOR,
        overlay_position: str = "bottom-center",
        margen: int = MARGEN_INFERIOR) -> tuple[int, int]:
    """Calcula una de ocho anclas y la mantiene dentro de la pantalla Tk."""
    posiciones = {
        "top-left", "top-center", "top-right",
        "middle-left", "middle-right",
        "bottom-left", "bottom-center", "bottom-right",
    }
    if overlay_position not in posiciones:
        raise ValueError(f"posición de overlay inválida: {overlay_position!r}")

    ancho_pantalla = max(0, int(ancho_pantalla))
    alto_pantalla = max(0, int(alto_pantalla))
    ancho_ventana = max(1, int(ancho_ventana))
    alto_ventana = max(1, int(alto_ventana))
    margen = max(0, int(margen))
    max_x = max(0, ancho_pantalla - ancho_ventana)
    max_y = max(0, alto_pantalla - alto_ventana)

    vertical, horizontal = overlay_position.split("-", 1)
    posiciones_x = {
        "left": margen,
        "center": (ancho_pantalla - ancho_ventana) // 2,
        "right": ancho_pantalla - ancho_ventana - margen,
    }
    posiciones_y = {
        "top": margen,
        "middle": (alto_pantalla - alto_ventana) // 2,
        "bottom": alto_pantalla - alto_ventana - margen,
    }
    x = min(max_x, max(0, posiciones_x[horizontal]))
    y = min(max_y, max(0, posiciones_y[vertical]))
    return x, y


class Indicador:
    def __init__(self, *, estado_ui=None, overlay_position="bottom-center"):
        import tkinter as tk
        self.tk = tk
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(takefocus=False)
        self._alpha_disponible = False
        try:
            self.root.attributes("-alpha", OPACIDAD_MAXIMA)
            self._alpha_disponible = True
        except Exception:
            pass
        self._opacidad = OPACIDAD_MAXIMA
        self._overlay_position = overlay_position
        self.canvas = tk.Canvas(
            self.root,
            width=ANCHO_INDICADOR,
            height=ALTO_INDICADOR,
            highlightthickness=0,
            borderwidth=0,
            bg=FONDO,
            takefocus=0,
        )
        self.canvas.pack()
        self._contenedor = self.canvas.create_rectangle(
            0.5,
            0.5,
            ANCHO_INDICADOR - 0.5,
            ALTO_INDICADOR - 0.5,
            outline=BORDE,
            width=1,
        )
        self.root.update_idletasks()
        self._aplicar_geometria()
        centro_y = ALTO_INDICADOR // 2
        self._barras = [
            self.canvas.create_line(
                PADDING_HORIZONTAL + ANCHO_BARRA / 2 + indice * PASO_BARRAS,
                centro_y - ALTURA_REPOSO / 2,
                PADDING_HORIZONTAL + ANCHO_BARRA / 2 + indice * PASO_BARRAS,
                centro_y + ALTURA_REPOSO / 2,
                fill=COLORES["idle"],
                width=ANCHO_BARRA,
                capstyle=tk.ROUND,
            )
            for indice in range(CANTIDAD_BARRAS)
        ]
        self._fase = 0.0
        self._nivel_suavizado = 0.0
        self._umbral_activo = False
        self._pico_visual = PICO_VISUAL_MINIMO
        self._forma_suavizada = ENVOLVENTE_REPOSO
        self._alturas_actuales = _ALTURAS_REPOSO
        self._color_actual = COLORES["idle"]
        self._estado_ui = estado_ui or EstadoInterfaz()
        self._ventana_visible = False
        self._salir = False
        self.root.after(INTERVALO_ANIMACION_MS, self._sondear)

    def _aplicar_geometria(self):
        self.root.update_idletasks()
        x, y = calcular_posicion(
            self.root.winfo_screenwidth(),
            self.root.winfo_screenheight(),
            ANCHO_INDICADOR,
            ALTO_INDICADOR,
            self._overlay_position,
            MARGEN_INFERIOR,
        )
        self.root.geometry(
            f"{ANCHO_INDICADOR}x{ALTO_INDICADOR}+{x}+{y}")

    def fijar_estado(self, estado: str):
        self._estado_ui.fijar_operativo(estado)

    def preparar_generacion(self, generacion: int):
        return self._estado_ui.preparar_generacion(generacion)

    def iniciar_captura(self, generacion: int):
        return self._estado_ui.iniciar_captura(generacion)

    def cerrar_captura(
            self, generacion: int, *, trabajo_pendiente: bool = False):
        return self._estado_ui.cerrar_captura(
            generacion, trabajo_pendiente=trabajo_pendiente)

    def iniciar_trabajo(self, generacion: int):
        return self._estado_ui.iniciar_trabajo(generacion)

    def finalizar_trabajo(self, generacion: int):
        return self._estado_ui.finalizar_trabajo(generacion)

    def invalidar_generacion(self, generacion: int):
        return self._estado_ui.invalidar_generacion(generacion)

    def completar_generacion(
            self, generacion: int, *, con_error: bool = False):
        return self._estado_ui.completar_generacion(
            generacion, con_error=con_error)

    def fijar_continuo(self, activo: bool):
        self._estado_ui.fijar_continuo(activo)

    def fijar_presionado(self, activo: bool):
        self._estado_ui.fijar_presionado(activo)

    def snapshot_ui(self):
        return self._estado_ui.snapshot()

    def cerrar(self):
        self._estado_ui.cerrar()
        self._salir = True

    def _fijar_opacidad(self, opacidad: float):
        self._opacidad = min(OPACIDAD_MAXIMA, max(0.0, opacidad))
        if not self._alpha_disponible:
            return
        try:
            self.root.attributes("-alpha", self._opacidad)
        except Exception:
            self._alpha_disponible = False
            self._opacidad = OPACIDAD_MAXIMA

    def _avanzar_fade(self, visible: bool):
        if not self._alpha_disponible:
            self._opacidad = OPACIDAD_MAXIMA if visible else 0.0
            return
        pasos = max(1.0, FADE_MS / INTERVALO_ANIMACION_MS)
        paso = OPACIDAD_MAXIMA / pasos
        destino = OPACIDAD_MAXIMA if visible else 0.0
        if visible:
            self._fijar_opacidad(min(destino, self._opacidad + paso))
        else:
            self._fijar_opacidad(max(destino, self._opacidad - paso))

    def _ocultar_y_reiniciar(self):
        self.root.withdraw()
        self._ventana_visible = False
        self._fase = 0.0
        self._nivel_suavizado = 0.0
        self._umbral_activo = False
        self._pico_visual = PICO_VISUAL_MINIMO
        self._forma_suavizada = ENVOLVENTE_REPOSO
        self._alturas_actuales = _ALTURAS_REPOSO
        self._fijar_opacidad(OPACIDAD_MAXIMA)

    def _renderizar(
            self,
            operativo: str,
            nivel_visual: float,
            envolvente_visual: tuple[float, ...] = ENVOLVENTE_REPOSO):
        if operativo == "recording":
            procesado = procesar_nivel_visual(
                nivel_visual,
                self._nivel_suavizado,
                self._umbral_activo,
                self._pico_visual,
            )
            self._nivel_suavizado = procesado.nivel
            self._umbral_activo = procesado.umbral_activo
            self._pico_visual = procesado.pico_visual
            objetivo_forma = (
                normalizar_forma_local(envolvente_visual)
                if procesado.umbral_activo
                else ENVOLVENTE_REPOSO
            )
            self._forma_suavizada = suavizar_forma_barras(
                objetivo_forma,
                self._forma_suavizada,
                procesado.umbral_activo,
            )
        frame = calcular_frame_waveform(
            operativo,
            self._fase,
            self._nivel_suavizado,
            self._forma_suavizada,
        )
        factores = {
            "recording": 0.55,
            "transcribing": 0.24,
            "error": 0.42,
        }
        self._alturas_actuales = interpolar_alturas(
            self._alturas_actuales,
            frame.alturas,
            factores.get(operativo, 0.40),
        )
        for barra, altura in zip(self._barras, self._alturas_actuales):
            x, _, _, _ = self.canvas.coords(barra)
            y_inicial = (ALTO_INDICADOR - altura) // 2
            self.canvas.coords(barra, x, y_inicial, x, y_inicial + altura)
        if frame.color != self._color_actual:
            for barra in self._barras:
                self.canvas.itemconfig(barra, fill=frame.color)
            self._color_actual = frame.color
        self._fase += INTERVALO_ANIMACION_MS / 1000.0

    def _sondear(self):
        if self._salir:
            self.root.destroy()
            return
        snapshot = self._estado_ui.snapshot()
        if snapshot.visible:
            self._renderizar(
                snapshot.operativo,
                snapshot.nivel_visual,
                snapshot.envolvente_visual,
            )
            if not self._ventana_visible:
                # Algunos WM descartan la posición solicitada mientras la raíz
                # está retirada. Reaplicarla en la transición evita (0, 0).
                self._aplicar_geometria()
                if self._alpha_disponible:
                    self._fijar_opacidad(0.0)
                self.root.deiconify()
                self._aplicar_geometria()
                self._ventana_visible = True
            self._avanzar_fade(True)
        else:
            if self._ventana_visible:
                self._avanzar_fade(False)
                if not self._alpha_disponible or self._opacidad <= 0.0:
                    self._ocultar_y_reiniciar()
        intervalo = calcular_intervalo_sondeo(
            snapshot.visible, self._ventana_visible)
        self.root.after(intervalo, self._sondear)

    def ejecutar(self):
        self.root.mainloop()


class BucleSinUI:
    """Mantiene vivo el hilo principal cuando el indicador no está disponible."""

    def __init__(self, *, estado_ui=None):
        self._salir = False
        self._estado_ui = estado_ui or EstadoInterfaz()

    def fijar_estado(self, estado: str):
        self._estado_ui.fijar_operativo(estado)

    def preparar_generacion(self, generacion: int):
        return self._estado_ui.preparar_generacion(generacion)

    def iniciar_captura(self, generacion: int):
        return self._estado_ui.iniciar_captura(generacion)

    def cerrar_captura(
            self, generacion: int, *, trabajo_pendiente: bool = False):
        return self._estado_ui.cerrar_captura(
            generacion, trabajo_pendiente=trabajo_pendiente)

    def iniciar_trabajo(self, generacion: int):
        return self._estado_ui.iniciar_trabajo(generacion)

    def finalizar_trabajo(self, generacion: int):
        return self._estado_ui.finalizar_trabajo(generacion)

    def invalidar_generacion(self, generacion: int):
        return self._estado_ui.invalidar_generacion(generacion)

    def completar_generacion(
            self, generacion: int, *, con_error: bool = False):
        return self._estado_ui.completar_generacion(
            generacion, con_error=con_error)

    def fijar_continuo(self, activo: bool):
        self._estado_ui.fijar_continuo(activo)

    def fijar_presionado(self, activo: bool):
        self._estado_ui.fijar_presionado(activo)

    def snapshot_ui(self):
        return self._estado_ui.snapshot()

    def cerrar(self):
        self._estado_ui.cerrar()
        self._salir = True

    def ejecutar(self):
        while not self._salir:
            time.sleep(0.2)


def crear_ui(habilitado: bool, *, estado_ui=None,
             overlay_position="bottom-center"):
    if habilitado:
        try:
            return Indicador(
                estado_ui=estado_ui,
                overlay_position=overlay_position,
            )
        except Exception as e:
            print(f"[indicador] no disponible ({e}); corriendo sin UI")
    return BucleSinUI(estado_ui=estado_ui)
