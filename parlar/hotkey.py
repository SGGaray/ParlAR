"""Modelo puro y validación de combinaciones de teclado de ParlAR.

La representación persistida sigue la sintaxis de ``pynput.HotKey``. Este
módulo no importa pynput ni Tk: traduce esa sintaxis a un modelo normalizado
y a etiquetas humanas, y puede usarse durante la carga de configuración.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable, Iterable

ATAJO_PREDETERMINADO = "<ctrl_r>+<cmd_r>"


class ErrorAtajo(ValueError):
    """La combinación no puede usarse como atajo principal."""


@dataclass(frozen=True, slots=True)
class TeclaAtajo:
    token: str
    identidad: tuple[str, str]
    modificador: bool
    orden: tuple[int, int, str]
    etiqueta: str


@dataclass(frozen=True, slots=True)
class AtajoNormalizado:
    teclas: tuple[TeclaAtajo, ...]

    @property
    def persistido(self) -> str:
        return "+".join(tecla.token for tecla in self.teclas)

    @property
    def etiqueta(self) -> str:
        return " + ".join(tecla.etiqueta for tecla in self.teclas)


_MODIFICADORES = {
    "ctrl": ("ctrl", "left", 0, 0, "Ctrl"),
    "ctrl_l": ("ctrl", "left", 0, 0, "Ctrl izquierdo"),
    "ctrl_r": ("ctrl", "right", 0, 1, "Ctrl derecho"),
    "alt": ("alt", "left", 1, 0, "Alt"),
    "alt_l": ("alt", "left", 1, 0, "Alt izquierdo"),
    "alt_r": ("alt", "right", 1, 1, "Alt derecho"),
    "alt_gr": ("alt_gr", "right", 1, 2, "AltGr"),
    "shift": ("shift", "left", 2, 0, "Mayús"),
    "shift_l": ("shift", "left", 2, 0, "Mayús izquierda"),
    "shift_r": ("shift", "right", 2, 1, "Mayús derecha"),
    "cmd": ("meta", "left", 3, 0, "Meta"),
    "cmd_l": ("meta", "left", 3, 0, "Meta izquierda"),
    "cmd_r": ("meta", "right", 3, 1, "Meta derecha"),
}

_ESPECIALES = {
    "backspace": "Retroceso",
    "caps_lock": "Bloq Mayús",
    "delete": "Suprimir",
    "down": "Flecha abajo",
    "end": "Fin",
    "enter": "Enter",
    "esc": "Escape",
    "home": "Inicio",
    "insert": "Insertar",
    "left": "Flecha izquierda",
    "menu": "Menú",
    "num_lock": "Bloq Num",
    "page_down": "Av Pág",
    "page_up": "Re Pág",
    "pause": "Pausa",
    "print_screen": "Impr Pant",
    "right": "Flecha derecha",
    "scroll_lock": "Bloq Despl",
    "space": "Espacio",
    "tab": "Tab",
    "up": "Flecha arriba",
    "media_play_pause": "Reproducir/Pausa",
    "media_volume_mute": "Silenciar",
    "media_volume_down": "Bajar volumen",
    "media_volume_up": "Subir volumen",
    "media_previous": "Pista anterior",
    "media_next": "Pista siguiente",
}
_ESPECIALES.update({f"f{numero}": f"F{numero}" for numero in range(1, 21)})

_KEYSYMS_TK = {
    "Control_L": "<ctrl_l>",
    "Control_R": "<ctrl_r>",
    "Shift_L": "<shift_l>",
    "Shift_R": "<shift_r>",
    "Alt_L": "<alt_l>",
    "Alt_R": "<alt_r>",
    "Meta_L": "<cmd_l>",
    "Meta_R": "<cmd_r>",
    "Super_L": "<cmd_l>",
    "Super_R": "<cmd_r>",
    "ISO_Level3_Shift": "<alt_gr>",
    "Escape": "<esc>",
    "Return": "<enter>",
    "KP_Enter": "<enter>",
    "BackSpace": "<backspace>",
    "Delete": "<delete>",
    "Insert": "<insert>",
    "Home": "<home>",
    "End": "<end>",
    "Left": "<left>",
    "Right": "<right>",
    "Up": "<up>",
    "Down": "<down>",
    "Prior": "<page_up>",
    "Next": "<page_down>",
    "space": "<space>",
    "Tab": "<tab>",
}
_KEYSYMS_TK.update({f"F{numero}": f"<f{numero}>" for numero in range(1, 21)})


def _partes_pynput(texto: str) -> tuple[str, ...]:
    """Replica el separador de pynput, incluido ``+`` como tecla literal."""
    partes = []
    inicio = 0
    for indice, caracter in enumerate(texto):
        if caracter == "+" and indice != inicio:
            partes.append(texto[inicio:indice])
            inicio = indice + 1
    if inicio == len(texto):
        raise ErrorAtajo("La combinación termina de forma incompleta.")
    partes.append(texto[inicio:])
    return tuple(partes)


def _tecla_desde_token(token: str) -> TeclaAtajo:
    if len(token) == 1:
        if not token.isprintable() or token.isspace():
            raise ErrorAtajo("La combinación contiene una tecla no válida.")
        normalizado = token.casefold()
        return TeclaAtajo(
            normalizado,
            ("char", normalizado),
            False,
            (4, 0, normalizado),
            normalizado.upper(),
        )

    if len(token) <= 2 or token[0] != "<" or token[-1] != ">":
        raise ErrorAtajo(f"Sintaxis de tecla no válida: {token!r}.")
    nombre = token[1:-1].lower()
    if nombre in _MODIFICADORES:
        grupo, lado, categoria, orden_lado, etiqueta = _MODIFICADORES[nombre]
        return TeclaAtajo(
            f"<{nombre}>",
            (grupo, lado),
            True,
            (categoria, orden_lado, nombre),
            etiqueta,
        )
    if nombre in _ESPECIALES:
        return TeclaAtajo(
            f"<{nombre}>",
            ("special", nombre),
            False,
            (4, 1, nombre),
            _ESPECIALES[nombre],
        )
    try:
        codigo = int(nombre)
    except ValueError as exc:
        raise ErrorAtajo(f"Tecla especial desconocida: <{nombre}>.") from exc
    if codigo <= 0:
        raise ErrorAtajo("El código virtual de tecla debe ser positivo.")
    return TeclaAtajo(
        f"<{codigo}>",
        ("vk", str(codigo)),
        False,
        (4, 2, str(codigo).zfill(10)),
        f"Tecla {codigo}",
    )


def parsear_atajo(texto: str) -> AtajoNormalizado:
    if type(texto) is not str or not texto:
        raise ErrorAtajo("El atajo no puede estar vacío.")
    teclas = tuple(_tecla_desde_token(parte) for parte in _partes_pynput(texto))
    identidades = [tecla.identidad for tecla in teclas]
    if len(identidades) != len(set(identidades)):
        raise ErrorAtajo("La combinación contiene teclas duplicadas.")
    return AtajoNormalizado(tuple(sorted(teclas, key=lambda tecla: tecla.orden)))


def _firma_conflicto(atajo: AtajoNormalizado) -> frozenset[tuple[str, str]]:
    firma = set()
    for tecla in atajo.teclas:
        if tecla.modificador:
            firma.add(("modifier", tecla.identidad[0]))
        else:
            firma.add(tecla.identidad)
    return frozenset(firma)


def validar_atajo_principal(
        texto: str,
        *,
        hotkey_salida: str = "<ctrl>+<alt>+q",
        reservados: Iterable[str] = (),
) -> AtajoNormalizado:
    atajo = parsear_atajo(texto)
    if len(atajo.teclas) < 2:
        if atajo.teclas and not atajo.teclas[0].modificador:
            raise ErrorAtajo("Usá al menos un modificador junto con la tecla.")
        raise ErrorAtajo("Usá una combinación de al menos dos teclas.")
    if any(tecla.identidad == ("special", "esc") for tecla in atajo.teclas):
        raise ErrorAtajo("Escape está reservado para cancelar el dictado.")
    if not any(tecla.modificador for tecla in atajo.teclas):
        raise ErrorAtajo("La combinación debe incluir al menos un modificador.")
    if sum(not tecla.modificador for tecla in atajo.teclas) > 1:
        raise ErrorAtajo("Usá como máximo una tecla común en la combinación.")

    firma = _firma_conflicto(atajo)
    try:
        salida = parsear_atajo(hotkey_salida)
    except ErrorAtajo as exc:
        raise ErrorAtajo("El atajo interno de salida no es válido.") from exc
    if firma == _firma_conflicto(salida):
        raise ErrorAtajo("La combinación está reservada para salir de ParlAR.")
    for reservado in reservados:
        if firma == _firma_conflicto(parsear_atajo(reservado)):
            raise ErrorAtajo("La combinación está reservada por ParlAR.")
    return atajo


def etiqueta_atajo(texto: str) -> str:
    return parsear_atajo(texto).etiqueta


def token_desde_evento_tk(keysym: str, caracter: str = "") -> str | None:
    """Traduce un evento Tk a sintaxis interna sin usar el label visible."""
    if keysym in _KEYSYMS_TK:
        return _KEYSYMS_TK[keysym]
    if len(keysym) > 1 and keysym.startswith("KP_") and caracter:
        return caracter.casefold() if len(caracter) == 1 else None
    if caracter and len(caracter) == 1 and caracter.isprintable():
        return caracter.casefold()
    return None


class CapturaAtajo:
    """Acumula un chord físico y filtra autorepeat sin depender de Tk."""

    def __init__(self):
        self._presionadas: set[tuple[str, str]] = set()
        self._candidatas: dict[tuple[str, str], TeclaAtajo] = {}

    @property
    def candidata(self) -> AtajoNormalizado | None:
        if not self._candidatas:
            return None
        return AtajoNormalizado(tuple(sorted(
            self._candidatas.values(), key=lambda tecla: tecla.orden)))

    def presionar(self, token: str | None) -> str:
        if token is None or token == "<tab>":
            return "ignorar"
        tecla = _tecla_desde_token(token)
        if tecla.identidad == ("special", "esc"):
            return "cancelar"
        if tecla.identidad in self._presionadas:
            return "repeticion"
        if not self._presionadas and self._candidatas:
            self._candidatas.clear()
        self._presionadas.add(tecla.identidad)
        self._candidatas[tecla.identidad] = tecla
        return "capturada"

    def soltar(self, token: str | None) -> None:
        if token is None:
            return
        try:
            tecla = _tecla_desde_token(token)
        except ErrorAtajo:
            return
        self._presionadas.discard(tecla.identidad)


class SesionCapturaAtajo:
    """Ciclo puro del recorder con restauración idempotente de su lease."""

    def __init__(
            self,
            valor_anterior: str,
            *,
            validar: Callable[[str], AtajoNormalizado],
            suspension,
    ):
        self.valor_anterior = valor_anterior
        self.validar = validar
        self.suspension = suspension
        self.captura = CapturaAtajo()
        self.activa = False

    def iniciar(self, *, runtime_activo: bool) -> None:
        self.suspension.adquirir(runtime_activo=runtime_activo)
        self.activa = True

    def presionar(self, token: str | None) -> str:
        if not self.activa:
            return "ignorar"
        accion = self.captura.presionar(token)
        if accion == "cancelar":
            self.cancelar()
        return accion

    def soltar(self, token: str | None) -> None:
        if self.activa:
            self.captura.soltar(token)

    def usar(self) -> AtajoNormalizado:
        candidata = self.captura.candidata
        if candidata is None:
            raise ErrorAtajo("Todavía no se capturó una combinación.")
        try:
            normalizada = self.validar(candidata.persistido)
        except ErrorAtajo:
            raise
        except BaseException:
            self.cerrar()
            raise
        self.cerrar()
        return normalizada

    def renovar(self) -> bool:
        return self.activa and self.suspension.renovar()

    def cancelar(self) -> str:
        self.cerrar()
        return self.valor_anterior

    def perder_foco(self) -> str:
        return self.cancelar()

    def cerrar(self) -> None:
        if not self.activa:
            return
        self.activa = False
        self.suspension.liberar()
