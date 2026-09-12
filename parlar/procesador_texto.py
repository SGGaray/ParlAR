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
    (re.compile(r"^(nuevo p[aá]rrafo|punto y aparte|new paragraph)$", re.I), ("nueva_linea", "\n\n")),
    (re.compile(r"^(nueva l[ií]nea|new line)$", re.I), ("nueva_linea", "\n")),
    (re.compile(r"^(borra la [uú]ltima oraci[oó]n|borrar( la)? [uú]ltima oraci[oó]n|"
                r"delete last sentence)$", re.I), ("borrar_ultima", None)),
    (re.compile(r"^(detener dictado|parar dictado|stop dictation|stop listening)$", re.I),
     ("detener", None)),
    (re.compile(r"^(enviar|send message|send)$", re.I), ("enviar", None)),
]

_FIN_ORACION = re.compile(r"([.!?])\s+([¿¡]?)(\w)")
_TOKEN_NO_ESPACIO = re.compile(r"\S+")
_MARCADOR_ESTRUCTURA = re.compile(r"\ue000PARLARX*\d+\ue001")
_ESTRUCTURA_NUMERICA = re.compile(
    r"^[+-]?[€$£¥]?\d+(?:[.,:]\d+)+(?:%|[a-zA-Z]+)?$|"
    r"^[+-]?[€$£¥]?\d+(?:%|[a-zA-Z]+)$|^\d{2,4}(?:-\d{1,2}){1,2}$"
)
_LINEA_ESTRUCTURADA = re.compile(
    r"(?:^|\s)[A-Za-z_]\w*\s*=|^[\[{].*[\]}]$", re.DOTALL
)
_PARES_CITAS = {
    '"': '"', "'": "'", "“": "”", "‘": "’", "«": "»",
}


@dataclass
class Procesado:
    texto: str = ""
    comando: Optional[str] = None   # nueva_linea | borrar_ultima | detener | enviar
    carga: Optional[str] = None     # ej. "\n" para comandos de nueva línea


def _normalizar_espaciado(texto: str) -> str:
    texto = re.sub(r"\s+", " ", texto).strip()
    texto = re.sub(r"\s+([,.;:!?])", r"\1", texto)      # sin espacio antes de puntuación de cierre
    texto = re.sub(r"([,.;:!?])(\w)", r"\1 \2", texto)  # espacio después de puntuación
    # signos de apertura del español: espacio antes, nunca después
    texto = re.sub(r"([,.;:!?])([¿¡])", r"\1 \2", texto)  # "bien.¿y" -> "bien. ¿y"
    texto = re.sub(r"([¿¡])\s+", r"\1", texto)          # "¿ cómo" -> "¿cómo"
    texto = re.sub(r"(\w)([¿¡])", r"\1 \2", texto)      # "hola¿qué" -> "hola ¿qué"
    return texto


def _es_token_estructurado(token: str) -> bool:
    """Reconoce sintaxis donde puntuación/caso son datos, no prosa.

    La regla es deliberadamente amplia: ante un token ambiguo como
    ``test.it`` se prioriza fidelidad y se deja intacto.
    """
    if not token:
        return False
    nucleo = token.rstrip(",.;:!?") or token
    if _ESTRUCTURA_NUMERICA.fullmatch(nucleo):
        return True
    if token.isupper() and any(caracter.isalpha() for caracter in token):
        return True
    if any(marca in token for marca in ("://", "@", "/", "_", "+", "#")):
        return True
    if any(marca in token for marca in ("[", "]", "{", "}", "=", "\\")):
        return True
    if re.search(r"\w\.\w", token, re.UNICODE):
        return True
    if re.search(r"\w-\w", token, re.UNICODE):
        return True
    return bool(re.search(r"\w\([^\s)]*\)", token, re.UNICODE))


def _proteger_regiones_citadas(texto: str, guardar) -> str:
    """Protege pares de citas en una pasada, tolerando escapes con barra."""
    def esta_escapada(posicion: int) -> bool:
        barras = 0
        posicion -= 1
        while posicion >= 0 and texto[posicion] == "\\":
            barras += 1
            posicion -= 1
        return barras % 2 == 1

    salida = []
    inicio_copia = 0
    indice = 0
    largo = len(texto)
    while indice < largo:
        apertura = texto[indice]
        cierre = _PARES_CITAS.get(apertura)
        if cierre is None:
            indice += 1
            continue
        if apertura in "\"'" and esta_escapada(indice):
            indice += 1
            continue
        if (apertura == "'" and indice > 0
                and (texto[indice - 1].isalnum() or texto[indice - 1] == "_")):
            indice += 1
            continue
        final = indice + 1
        while final < largo:
            if apertura in "\"'" and texto[final] == "\\":
                final += 2
                continue
            if texto[final] == cierre:
                if (cierre == "'" and final + 1 < largo
                        and (texto[final + 1].isalnum()
                             or texto[final + 1] == "_")):
                    final += 1
                    continue
                break
            final += 1
        if final >= largo:
            indice += 1
            continue
        salida.append(texto[inicio_copia:indice])
        salida.append(guardar(texto[indice:final + 1]))
        indice = final + 1
        inicio_copia = indice
    salida.append(texto[inicio_copia:])
    return "".join(salida)


def _proteger_estructura(texto: str):
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

    if _LINEA_ESTRUCTURADA.search(texto):
        return guardar(texto), reemplazos

    texto = _proteger_regiones_citadas(texto, guardar)

    def proteger_token(match):
        token = match.group(0)
        return guardar(token) if _es_token_estructurado(token) else token

    return _TOKEN_NO_ESPACIO.sub(proteger_token, texto), reemplazos


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
    """Quita muletillas sin confundir acrónimos en mayúsculas."""
    eliminadas = 0

    def quitar(match):
        nonlocal eliminadas
        if match.group(1).isupper():
            return match.group(0)
        eliminadas += 1
        return ""

    return MULETILLAS.sub(quitar, texto), eliminadas


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

    # ---------------------------------------------------------------- público

    def procesar_frase(self, crudo: str) -> Procesado:
        crudo = crudo.strip()
        if not crudo:
            return Procesado()

        if self.remove_fillers:
            solo_muletilla = _SOLO_MULETILLA.match(crudo)
            if solo_muletilla and not solo_muletilla.group(1).isupper():
                return Procesado()

        if self.voice_commands:
            cmd = self._buscar_comando(crudo)
            if cmd is not None:
                return cmd

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
        """Limpieza liviana para palabras incrementales (ya confirmadas). Sin
        reescritura a nivel oración porque la oración puede estar incompleta."""
        texto, estructura = _proteger_estructura(crudo)
        if self.remove_fillers:
            texto, _eliminadas = _quitar_muletillas(texto)
        texto = re.sub(r" {2,}", " ", texto)
        return _restaurar_estructura(texto, estructura)

    # ---------------------------------------------------------------- interno

    def _buscar_comando(self, crudo: str) -> Optional[Procesado]:
        if self._frase_completamente_citada(crudo):
            return None
        norm = re.sub(r"[^\w\sáéíóúñü]", "", crudo).strip().lower()
        for pat, (cmd, carga) in _PATRONES_CMD:
            if pat.match(norm):
                return Procesado(comando=cmd, carga=carga)
        return None

    @staticmethod
    def _frase_completamente_citada(crudo: str) -> bool:
        """Acepta puntuación de oración después de la comilla de cierre."""
        if len(crudo) < 2 or crudo[0] not in _PARES_CITAS:
            return False
        final = len(crudo)
        while final > 0 and crudo[final - 1] in ".?!":
            final -= 1
        return final > 1 and crudo[final - 1] == _PARES_CITAS[crudo[0]]

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
            # muletillas discursivas: español primero, inglés de respaldo
            protegido, n_general = re.subn(
                r"\b(b[aá]sicamente|literalmente|o sea|digamos|basically|"
                r"actually|literally|you know|i mean|kind of|sort of)\b[,]?\s*",
                "", protegido, flags=re.IGNORECASE)
            # "viste" es ambiguo como verbo: solo se quita cuando una coma lo
            # separa inequívocamente como marcador discursivo.
            protegido, n_viste_inicio = re.subn(
                r"^\s*viste\s*,\s*", "", protegido,
                flags=re.IGNORECASE)
            protegido, n_viste_final = re.subn(
                r"\s*,\s*viste([.!?]?)\s*$", r"\1", protegido,
                flags=re.IGNORECASE)
            if not (n_general or n_viste_inicio or n_viste_final):
                return texto
            protegido = _normalizar_espaciado(protegido)
            protegido = _capitalizar_oraciones(protegido, inicio=True)
            return _restaurar_estructura(protegido, estructura)
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
