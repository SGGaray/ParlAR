"""Post-procesamiento de texto: limpieza, comandos de voz, modos de reescritura.

Whisper ya emite puntuación y mayúsculas; esta capa normaliza los bordes
(espaciado, mayúsculas de oración, muletillas), interpreta comandos de voz y
opcionalmente reescribe vía reglas o un endpoint HTTP(S) de Ollama. El default
es local; elegir una URL remota envía allí el texto a reescribir.

Adaptado al español: maneja signos de apertura ¿ ¡ (espaciado y mayúsculas)
y trae reglas de reescritura para español además de inglés. Los comandos de
voz son español-primero, con equivalentes en inglés como respaldo.
"""

import json
import re
import sys
import urllib.request
from dataclasses import dataclass
from typing import Optional

MULETILLAS = re.compile(
    r"\b(um+|uh+|erm+|hmm+|eh+|este{2,}|em|mmm+|ehm)\b[,.]?\s*", re.IGNORECASE
)
# "este" (una sola e) es el demostrativo español normal ("quiero este
# informe"), no una muletilla. Solo las formas alargadas ("esteee") lo son.
# Frase que es ÚNICAMENTE una muletilla suelta ("eh.", "mmm"): se descarta
# entera en vez de dejar un resto vacío tras MULETILLAS.sub().
_SOLO_MULETILLA = re.compile(
    r"^(um+|uh+|erm+|hmm+|eh+|em|mmm+|ehm)[,.]?$", re.IGNORECASE
)

# patrones de comandos de voz, comparados contra la frase completa normalizada
# (español primero, inglés como respaldo)
_PATRONES_CMD = [
    (re.compile(
        r"^(nuevo p[aá]rrafo|punto y aparte|salto de párrafo|new paragraph)$",
        re.I,
    ), ("nueva_linea", "\n\n")),
    (re.compile(
        r"^(nueva l[ií]nea|salto de línea|new line)$", re.I,
    ), ("nueva_linea", "\n")),
    (re.compile(r"^(borra la [uú]ltima oraci[oó]n|borrar( la)? [uú]ltima oraci[oó]n|"
                r"delete last sentence)$", re.I), ("borrar_ultima", None)),
    (re.compile(r"^(detener dictado|parar dictado|stop dictation|stop listening)$", re.I),
     ("detener", None)),
    (re.compile(
        r"^(enviar|mandar mensaje|enviar mensaje|send message|send)$", re.I,
    ), ("enviar", None)),
]
_PUNTUACION_TERMINAL_COMANDO = frozenset(".!?…")
_PARES_PUNTUACION_COMANDO = {"¡": "!", "¿": "?"}

_FIN_ORACION = re.compile(r"([.!?])\s+([¿¡]?)(\w)")
_MARCADOR_ESTRUCTURA = re.compile(r"\ue000PARLARX*\d+\ue001")
_ESTRUCTURA_NUMERICA = re.compile(
    r"^(?:(?:[€$£¥][+\-−]?)|(?:[+\-−][€$£¥]?))?"
    r"(?:\d+(?:[.,:]\d+)+|\.\d+|\d+)(?:%|[a-zA-Z]+)?$|"
    r"^\d{2,4}(?:-\d{1,2}){1,2}$"
)
_DOTFILE = re.compile(r"^\.[A-Za-z_][\w.-]*$", re.UNICODE)
_PARES_CITAS = {
    '"': '"', "'": "'", "“": "”", "‘": "’", "«": "»",
}
_PARES_DELIMITADORES = {"(": ")", "[": "]", "{": "}"}


@dataclass
class Procesado:
    texto: str = ""
    comando: Optional[str] = None   # nueva_linea | borrar_ultima | detener | enviar
    carga: Optional[str] = None     # ej. "\n" para comandos de nueva línea


@dataclass
class _EstadoLiteral:
    """Contexto acotado entre fragmentos de una misma unidad streaming."""

    cierre: Optional[str] = None
    escapado: bool = False


def _normalizar_espaciado(texto: str) -> str:
    texto = re.sub(r"\s+", " ", texto).strip()
    texto = re.sub(r"\s+([,.;:!?])", r"\1", texto)      # sin espacio antes de puntuación de cierre
    texto = re.sub(r"([,.;:!?])(\w)", r"\1 \2", texto)  # espacio después de puntuación
    # signos de apertura del español: espacio antes, nunca después
    texto = re.sub(r"([,.;:!?])([¿¡])", r"\1 \2", texto)  # "bien.¿y" -> "bien. ¿y"
    texto = re.sub(r"([¿¡])\s+", r"\1", texto)          # "¿ cómo" -> "¿cómo"
    texto = re.sub(r"(\w)([¿¡])", r"\1 \2", texto)      # "hola¿qué" -> "hola ¿qué"
    return texto


def _es_nucleo_estructurado(token: str) -> bool:
    if _ESTRUCTURA_NUMERICA.fullmatch(token) or _DOTFILE.fullmatch(token):
        return True
    if token.isupper() and any(caracter.isalpha() for caracter in token):
        return True
    if any(marca in token for marca in ("://", "@", "/", "_", "+", "#")):
        return True
    if any(marca in token for marca in ("[", "]", "{", "}", "=", "\\")):
        return True
    if "::" in token or re.search(r"\w:\w", token, re.UNICODE):
        return True
    if re.search(r"\w\.\w", token, re.UNICODE):
        return True
    if re.search(r"\w-\w", token, re.UNICODE):
        return True
    return bool(re.search(r"\w\([^\s)]*\)", token, re.UNICODE))


def _es_token_estructurado(token: str) -> bool:
    """Reconoce sintaxis donde puntuación/caso son datos, no prosa.

    La regla es deliberadamente amplia: ante un token ambiguo como
    ``test.it`` se prioriza fidelidad y se deja intacto.
    """
    if not token:
        return False
    if _es_nucleo_estructurado(token):
        return True
    exterior = token.rstrip(",;:!?")
    if exterior != token and _es_nucleo_estructurado(exterior):
        return True
    if token.endswith(".") and _es_nucleo_estructurado(token[:-1]):
        return True
    return False


def _proteger_regiones_citadas(
        texto: str, guardar, estado: Optional[_EstadoLiteral] = None) -> str:
    """Protege citas cerradas o abiertas en O(n), con contexto opcional."""
    cierre = estado.cierre if estado is not None else None
    escapado = estado.escapado if estado is not None else False
    salida = []
    inicio_copia = 0
    inicio_literal = 0 if cierre is not None else None
    indice = 0
    barras_fuera = 0
    while indice < len(texto):
        caracter = texto[indice]
        if cierre is not None:
            if escapado:
                escapado = False
            elif cierre in "\"'" and caracter == "\\":
                escapado = True
            elif caracter == cierre:
                if (cierre == "'" and indice + 1 < len(texto)
                        and (texto[indice + 1].isalnum()
                             or texto[indice + 1] == "_")):
                    indice += 1
                    continue
                salida.append(guardar(texto[inicio_literal:indice + 1]))
                inicio_copia = indice + 1
                inicio_literal = None
                cierre = None
            indice += 1
            continue

        posible_cierre = _PARES_CITAS.get(caracter)
        apertura_valida = posible_cierre is not None
        if caracter in "\"'" and barras_fuera % 2:
            apertura_valida = False
        if (caracter == "'" and indice > 0
                and (texto[indice - 1].isalnum()
                     or texto[indice - 1] == "_")):
            apertura_valida = False
        if apertura_valida:
            salida.append(texto[inicio_copia:indice])
            inicio_literal = indice
            cierre = posible_cierre
            escapado = False
            barras_fuera = 0
            indice += 1
            continue
        barras_fuera = barras_fuera + 1 if caracter == "\\" else 0
        indice += 1

    if inicio_literal is not None:
        salida.append(guardar(texto[inicio_literal:]))
        inicio_copia = len(texto)
    salida.append(texto[inicio_copia:])
    if estado is not None:
        estado.cierre = cierre
        estado.escapado = escapado
    return "".join(salida)


def _fin_region_balanceada(texto: str, apertura: int) -> int:
    """Devuelve el final exclusivo sin reexaminar el contenido recorrido."""
    pila = [_PARES_DELIMITADORES[texto[apertura]]]
    indice = apertura + 1
    while indice < len(texto) and pila:
        caracter = texto[indice]
        if caracter in _PARES_DELIMITADORES:
            pila.append(_PARES_DELIMITADORES[caracter])
        elif caracter == pila[-1]:
            pila.pop()
        indice += 1
    return indice


def _proteger_tokens_lineal(texto: str, guardar) -> str:
    """Embalsama tokens y expresiones balanceadas en una pasada lineal."""
    salida = []
    indice = 0
    while indice < len(texto):
        if texto[indice].isspace():
            salida.append(texto[indice])
            indice += 1
            continue

        inicio = indice
        if texto[indice] in _PARES_DELIMITADORES:
            indice = _fin_region_balanceada(texto, indice)
            while indice < len(texto) and not texto[indice].isspace():
                indice += 1
            salida.append(guardar(texto[inicio:indice]))
            continue

        if texto[indice].isalnum() or texto[indice] == "_":
            while (indice < len(texto)
                   and (texto[indice].isalnum()
                        or texto[indice] in "_.:+#/-")):
                indice += 1
            sonda = indice
            while sonda < len(texto) and texto[sonda] in " \t":
                sonda += 1
            if sonda < len(texto) and texto[sonda] == "=":
                final = texto.find("\n", sonda)
                indice = len(texto) if final < 0 else final
                salida.append(guardar(texto[inicio:indice]))
                continue
            if indice < len(texto) and texto[indice] in "([":
                indice = _fin_region_balanceada(texto, indice)
                while indice < len(texto) and not texto[indice].isspace():
                    indice += 1
                salida.append(guardar(texto[inicio:indice]))
                continue

        while indice < len(texto) and not texto[indice].isspace():
            indice += 1
        token = texto[inicio:indice]
        salida.append(guardar(token) if _es_token_estructurado(token) else token)
    return "".join(salida)


def _proteger_estructura(
        texto: str, estado: Optional[_EstadoLiteral] = None):
    """Protege regiones literales y tokens estructurados sin colisiones.

    La detección es deliberadamente conservadora: las citas completas y las
    líneas con forma de asignación/objeto se tratan como datos. Sobre el resto
    solo se embalsaman tokens inequívocamente técnicos o numéricos.
    """
    prefijo = "\ue000PARLAR"
    while prefijo in texto:
        prefijo += "X"
    reemplazos = {}

    def guardar(original):
        marcador = f"{prefijo}{len(reemplazos)}\ue001"
        reemplazos[marcador] = original
        return marcador

    texto = _proteger_regiones_citadas(texto, guardar, estado)
    return _proteger_tokens_lineal(texto, guardar), reemplazos


def _restaurar_estructura(texto: str, reemplazos) -> str:
    # Una cita puede quedar envuelta por el token de código que la contiene.
    # Como la profundidad máxima es dos, ambas capas se restauran linealmente.
    for _ in range(2):
        restaurado = _MARCADOR_ESTRUCTURA.sub(
            lambda match: reemplazos.get(match.group(0), match.group(0)), texto)
        if restaurado == texto:
            break
        texto = restaurado
    return texto


def _quitar_muletillas(texto: str):
    """Quita sólo muletillas al inicio real, no coincidencias interiores."""
    eliminadas = 0
    while True:
        inicio = len(texto) - len(texto.lstrip())
        match = MULETILLAS.match(texto, inicio)
        if match is None or match.group(1).isupper():
            break
        texto = texto[match.end():]
        eliminadas += 1
    return texto, eliminadas


def _capitalizar_oraciones(texto: str, *, inicio: bool = False) -> str:
    if not texto:
        return texto
    # inicio del texto, contemplando ¿ o ¡ inicial
    if texto[0] in "¿¡":
        if len(texto) > 1:
            texto = texto[0] + texto[1].upper() + texto[2:]
    elif inicio:
        texto = texto[0].upper() + texto[1:]
    return _FIN_ORACION.sub(
        lambda m: m.group(1) + " " + m.group(2) + m.group(3).upper(), texto
    )


class ProcesadorTexto:
    def __init__(self, remove_fillers: bool = True, voice_commands: bool = True,
                 rewrite_mode: str = "none", ollama_model: str = "",
                 ollama_url: str = "http://127.0.0.1:11434",
                 comando_enviar: bool = False):
        self.remove_fillers = remove_fillers
        self.voice_commands = voice_commands
        self.comando_enviar = comando_enviar
        self.rewrite_mode = rewrite_mode
        self.ollama_model = ollama_model
        self.ollama_url = ollama_url.rstrip("/")
        self._estado_literal = _EstadoLiteral()
        self._fragmento_al_inicio = True

    # ---------------------------------------------------------------- público

    def procesar_frase(self, crudo: str) -> Procesado:
        if self.voice_commands:
            cmd = self._buscar_comando(crudo)
            if cmd is not None:
                return cmd

        # Una unidad con estructura de líneas o tabs no es prosa plana ni una
        # orden desnuda. Se conserva carácter por carácter para
        # que la frontera de Return de la salida pueda aplicar su política.
        if any(marca in crudo for marca in ("\n", "\r", "\t")):
            return Procesado(texto=crudo)

        crudo = crudo.strip()
        if not crudo:
            return Procesado()

        if self.remove_fillers:
            solo_muletilla = _SOLO_MULETILLA.match(crudo)
            if solo_muletilla and not solo_muletilla.group(1).isupper():
                return Procesado()

        texto, estructura = _proteger_estructura(crudo)
        muletillas_eliminadas = 0
        if self.remove_fillers:
            texto, muletillas_eliminadas = _quitar_muletillas(texto)
        texto = _normalizar_espaciado(texto)
        texto = _capitalizar_oraciones(
            texto, inicio=bool(muletillas_eliminadas))
        texto = _restaurar_estructura(texto, estructura)

        if self.rewrite_mode != "none" and texto:
            texto = self._reescribir(texto)

        return Procesado(texto=texto)

    def procesar_fragmento(self, crudo: str) -> str:
        """Devuelve texto confirmado sin tratar el borde como frontera léxica.

        LocalAgreement es append-only: un fragmento no ofrece lookahead para
        decidir fillers, citas, escapes ni tokens incompletos. Toda decisión
        destructiva queda reservada a frases completas.
        """
        return crudo

    def iniciar_unidad(self):
        self._reiniciar_contexto_incremental()

    def finalizar_unidad(self):
        self._reiniciar_contexto_incremental()

    def cancelar_unidad(self):
        self._reiniciar_contexto_incremental()

    def _reiniciar_contexto_incremental(self):
        self._estado_literal = _EstadoLiteral()
        self._fragmento_al_inicio = True

    # ---------------------------------------------------------------- interno

    def _buscar_comando(self, crudo: str) -> Optional[Procesado]:
        # Gramática positiva: whitespace exterior, una frase exacta y, como
        # máximo, un signo terminal inequívoco o un par español completo.
        # Ningún otro delimitador se elimina para fabricar una orden.
        norm = crudo.strip()
        if norm and norm[0] in _PARES_PUNTUACION_COMANDO:
            cierre = _PARES_PUNTUACION_COMANDO[norm[0]]
            if len(norm) < 3 or norm[-1] != cierre:
                return None
            interior = norm[1:-1]
            if interior != interior.strip():
                return None
            norm = interior
        elif norm and norm[-1] in _PUNTUACION_TERMINAL_COMANDO:
            norm = norm[:-1].rstrip()
        for pat, (cmd, carga) in _PATRONES_CMD:
            if pat.fullmatch(norm):
                return Procesado(comando=cmd, carga=carga)
        return None

    def _reescribir(self, texto: str) -> str:
        if self.ollama_model:
            protegido, estructura = _proteger_estructura(texto)
            salida = self._reescribir_ollama(protegido)
            marcadores_visibles = [
                marcador for marcador in estructura if marcador in protegido
            ]
            if salida and all(
                    salida.count(marcador) == protegido.count(marcador)
                    for marcador in marcadores_visibles):
                return _restaurar_estructura(salida, estructura)
            # Respuesta vacía o servicio inaccesible: fallback determinista.
            # También se usa si el modelo pierde una región protegida.
        return self._reescribir_reglas(texto)

    def _reescribir_reglas(self, texto: str) -> str:
        protegido, estructura = _proteger_estructura(texto)
        modo = self.rewrite_mode
        if modo == "concise":
            # Sin parser lingüístico no hay una forma local segura de probar
            # que calificadores como "literalmente" o "kind of" sean ruido.
            # La limpieza acústica inequívoca ya ocurrió antes de esta etapa.
            return texto
        if modo in ("formal", "email"):
            # español
            subs_es = {
                r"\bok\b|\bokey\b|\bokay\b": "de acuerdo",
                r"\bporfa\b|\bporfis\b": "por favor",
                r"\bfinde\b": "fin de semana",
                r"\bpa'\b|\bpa\b(?=\s+\w)": "para",
            }
            # inglés (respaldo, inofensivo sobre texto en español)
            subs_en = {
                r"\bwanna\b": "want to", r"\bgonna\b": "going to",
                r"\bgotta\b": "have to", r"\bkinda\b": "somewhat",
                r"\byeah\b": "yes", r"\bnope\b": "no",
                r"\bcan't\b": "cannot", r"\bwon't\b": "will not",
                r"\bdon't\b": "do not", r"\bdoesn't\b": "does not",
                r"\bisn't\b": "is not", r"\bI'm\b": "I am",
                r"\bit's\b": "it is", r"\bthat's\b": "that is",
            }
            cambios = 0
            for pat, rep in subs_es.items():
                protegido, n = re.subn(
                    pat, rep, protegido, flags=re.IGNORECASE)
                cambios += n
            for pat, rep in subs_en.items():
                protegido, n = re.subn(
                    pat, rep, protegido,
                    flags=re.IGNORECASE if pat not in (r"\bI'm\b",) else 0)
                cambios += n
            if not cambios:
                return texto
            protegido = _capitalizar_oraciones(
                _normalizar_espaciado(protegido), inicio=True)
            return _restaurar_estructura(protegido, estructura)
        return texto

    def _reescribir_ollama(self, texto: str) -> Optional[str]:
        prompts = {
            "formal": "Reescribe el siguiente texto dictado en registro formal. "
                      "Mantén el significado y el idioma original. "
                      "Devuelve solo el texto reescrito.",
            "concise": "Reescribe el siguiente texto dictado de la forma más concisa posible. "
                       "Mantén el significado y el idioma original. "
                       "Devuelve solo el texto reescrito.",
            "email": "Reescribe el siguiente texto dictado como cuerpo de correo pulido. "
                     "Mantén el idioma original. Devuelve solo el texto reescrito.",
        }
        prompt = prompts.get(self.rewrite_mode)
        if not prompt:
            return None
        cuerpo = json.dumps({
            "model": self.ollama_model,
            "prompt": f"{prompt}\n\nTexto: {texto}",
            "stream": False,
            "options": {"temperature": 0.2},
        }).encode()
        try:
            req = urllib.request.Request(
                f"{self.ollama_url}/api/generate", data=cuerpo,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())
            return data.get("response", "").strip() or None
        except Exception as exc:
            print(
                f"[procesador] Ollama no disponible ({type(exc).__name__}); "
                "se usa fallback local",
                file=sys.stderr,
            )
            return None
