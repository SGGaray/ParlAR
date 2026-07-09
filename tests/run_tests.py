"""Tests de la lógica independiente de hardware: segmentación VAD,
procesamiento de texto, política de confirmación LocalAgreement (con motor
falso) y adaptaciones al español.

Correr: python tests/run_tests.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np

from parlar.capturador_audio import Segmentador
from parlar.procesador_texto import ProcesadorTexto
from parlar import motor_transcripcion as mt
from parlar.control import normalizar_comando

FALLAS = []


def check(nombre, cond, detalle=""):
    estado = "PASA" if cond else "FALLA"
    print(f"[{estado}] {nombre} {detalle}")
    if not cond:
        FALLAS.append(nombre)


# ---------------------------------------------------------------- Segmentador

class VADFalso:
    """VAD guionado: devuelve booleanos de una lista."""
    def __init__(self, guion):
        self.guion = list(guion)

    def is_speech(self, frame, sr):
        return self.guion.pop(0) if self.guion else False


def frame(ms=20, sr=16000, valor=1000):
    return (np.ones(sr * ms // 1000, dtype=np.int16) * valor).tobytes()


def test_segmentador():
    # 10 frames con voz, luego 40 en silencio (>600ms) => una frase
    guion = [True] * 10 + [False] * 40
    seg = Segmentador(16000, 20, VADFalso(guion), silence_ms=600, preroll_ms=100,
                      max_utterance_s=30, min_speech_ms=40)
    eventos = []
    for _ in range(50):
        eventos.extend(seg.procesar(frame()))
    tipos = [e.tipo for e in eventos]
    check("segmentador emite inicio_voz", "inicio_voz" in tipos)
    check("segmentador emite exactamente una frase", tipos.count("frase") == 1)
    fr = next(e for e in eventos if e.tipo == "frase")
    dur = fr.audio.size / 16000
    check("la frase incluye preroll y voz", 0.15 <= dur <= 1.2, f"dur={dur:.2f}s")
    check("audio de la frase es float32 en [-1,1]",
          fr.audio.dtype == np.float32 and float(np.abs(fr.audio).max()) <= 1.0)

    # un chispazo corto (bajo min_speech_ms) NO debe iniciar voz
    seg2 = Segmentador(16000, 20, VADFalso([True] + [False] * 20), silence_ms=600,
                       preroll_ms=100, max_utterance_s=30, min_speech_ms=100)
    ev2 = []
    for _ in range(21):
        ev2.extend(seg2.procesar(frame()))
    check("chispazo bajo min_speech ignorado", len(ev2) == 0)

    # el tope de frase corta incluso con voz continua
    seg3 = Segmentador(16000, 20, VADFalso([True] * 300), silence_ms=600,
                       preroll_ms=0, max_utterance_s=2.0, min_speech_ms=40)
    ev3 = []
    for _ in range(150):
        ev3.extend(seg3.procesar(frame()))
    check("el tope de frase máxima se dispara", any(e.tipo == "frase" for e in ev3))


# ---------------------------------------------------------------- ProcesadorTexto

def test_procesador_texto():
    p = ProcesadorTexto(remove_fillers=True, voice_commands=True)

    r = p.procesar_frase("um so this is , a test.it works")
    check("muletillas eliminadas", "um" not in r.texto.lower(), repr(r.texto))
    check("espaciado normalizado", ", a test. It works" in r.texto, repr(r.texto))
    check("oración capitalizada", r.texto.startswith("So"), repr(r.texto))

    r = p.procesar_frase("new paragraph")
    check("comando: new paragraph (compat)", r.comando == "nueva_linea" and r.carga == "\n\n")
    r = p.procesar_frase("Delete last sentence.")
    check("comando: delete last sentence (compat)", r.comando == "borrar_ultima")
    r = p.procesar_frase("nuevo párrafo")
    check("comando: nuevo párrafo", r.comando == "nueva_linea")
    r = p.procesar_frase("stop dictation")
    check("comando: stop (compat)", r.comando == "detener")

    pf = ProcesadorTexto(rewrite_mode="formal")
    r = pf.procesar_frase("yeah I'm gonna check, it's kinda late")
    t = r.texto.lower()
    check("reescritura formal (en)", "going to" in t and "gonna" not in t, repr(r.texto))

    pc = ProcesadorTexto(rewrite_mode="concise")
    r = pc.procesar_frase("basically this is actually the result you know")
    t = r.texto.lower()
    check("reescritura concisa (en)", "basically" not in t and "actually" not in t,
          repr(r.texto))

    check("entrada vacía segura", p.procesar_frase("   ").texto == "")


def test_espanol():
    p = ProcesadorTexto(remove_fillers=True, voice_commands=True)

    # signos de apertura: mayúscula tras ¿ inicial y tras fin de oración
    r = p.procesar_frase("¿cómo estás?todo bien.¿y vos?")
    check("mayúscula tras ¿ inicial", r.texto.startswith("¿Cómo"), repr(r.texto))
    check("espacio tras ? de cierre", "estás? Todo" in r.texto, repr(r.texto))
    check("mayúscula tras . con ¿", "bien. ¿Y vos?" in r.texto, repr(r.texto))

    # espaciado de aperturas: espacio antes, nunca después
    r = p.procesar_frase("hola¿ qué tal")
    check("aperturas: 'Hola ¿qué'", "Hola ¿qué" in r.texto, repr(r.texto))

    # comandos español-primero
    r = p.procesar_frase("punto y aparte")
    check("comando: punto y aparte", r.comando == "nueva_linea" and r.carga == "\n\n")
    r = p.procesar_frase("borra la última oración")
    check("comando: borra la última oración", r.comando == "borrar_ultima")
    r = p.procesar_frase("Enviar")
    check("comando: enviar (apagado por defecto, no se ejecuta)",
          r.comando is None and r.texto == "Enviar")
    p_enviar_on = ProcesadorTexto(comando_enviar=True)
    r = p_enviar_on.procesar_frase("Enviar")
    check("comando: enviar (activado explícitamente, sí se ejecuta)",
          r.comando == "enviar")
    r = p.procesar_frase("detener dictado")
    check("comando: detener dictado", r.comando == "detener")
    r = p.procesar_frase("nueva línea")
    check("comando: nueva línea", r.comando == "nueva_linea" and r.carga == "\n")

    # reescritura formal en español
    pf = ProcesadorTexto(rewrite_mode="formal")
    r = pf.procesar_frase("ok porfa mandame el informe el finde")
    t = r.texto.lower()
    check("formal es: ok/porfa/finde",
          "de acuerdo" in t and "por favor" in t and "fin de semana" in t, repr(r.texto))

    # reescritura concisa en español
    pc = ProcesadorTexto(rewrite_mode="concise")
    r = pc.procesar_frase("básicamente o sea esto funciona viste")
    t = r.texto.lower()
    check("conciso es: sin muletillas discursivas",
          "básicamente" not in t and "o sea" not in t and "viste" not in t, repr(r.texto))

    # alias de comandos de control
    check("alias control: toggle->alternar", normalizar_comando("toggle")[0] == "alternar")
    check("alias control: mode frase->utterance",
          normalizar_comando("mode frase") == ["modo", "utterance"])
    check("alias control: rewrite conciso->concise",
          normalizar_comando("rewrite conciso") == ["reescritura", "concise"])


# ---------------------------------------------------------------- Confirmación streaming

class PalabraFalsa:
    def __init__(self, word, end):
        self.word, self.end = word, end


class SegFalso:
    def __init__(self, words):
        self.words = words


class MotorFalso:
    """Devuelve hipótesis guionadas por decodificación, simulando un modelo
    cuyas palabras finales fluctúan entre pasadas (la razón real por la que
    existe LocalAgreement)."""
    def __init__(self, hipotesis):
        self.hips = list(hipotesis)
        self.llamadas = 0

    def decodificar(self, audio, word_timestamps=False, beam_size=None):
        h = self.hips[min(self.llamadas, len(self.hips) - 1)]
        self.llamadas += 1
        palabras = [PalabraFalsa(f" {w}", 0.4 * (i + 1)) for i, w in enumerate(h)]
        return [SegFalso(palabras)]


def test_confirmacion_streaming():
    hips = [
        ["hola"],                                # pasada 1
        ["hola", "mundo", "estp"],               # pasada 2: coincide en "hola"
        ["hola", "mundo", "esto", "es"],         # pasada 3: coincide hola mundo
        ["hola", "mundo", "esto", "es", "todo"], # pasada 4
    ]
    motor = MotorFalso(hips)
    st = mt.TranscriptorStreaming(motor, sample_rate=16000, interval_s=0.1, trim_s=999)

    confirmado = []
    for _ in range(4):
        st.aceptar_audio(np.zeros(16000, dtype=np.float32))  # 1s por pasada
        salida = st.procesar()
        confirmado.append(salida)

    check("pasada1 no confirma nada (sin hipótesis previa)", confirmado[0] == "")
    check("pasada2 confirma el prefijo 'hola'", confirmado[1].strip() == "hola",
          repr(confirmado[1]))
    check("pasada3 confirma solo 'mundo' (no la cola inestable)",
          confirmado[2].strip() == "mundo", repr(confirmado[2]))
    check("pasada4 confirma 'esto es' ya estabilizado",
          confirmado[3].strip() == "esto es", repr(confirmado[3]))
    check("ninguna palabra confirmada dos veces",
          " ".join("".join(confirmado).split()) == "hola mundo esto es")

    cola = st.finalizar()
    check("finalizar vacía la cola restante", cola.strip() == "todo", repr(cola))
    check("finalizar reinicia el buffer", st.buffer.size == 0)


def test_recorte_streaming():
    motor = MotorFalso([["a", "b", "c"]] * 10)
    st = mt.TranscriptorStreaming(motor, sample_rate=16000, interval_s=0.1, trim_s=2.0)
    for _ in range(4):
        st.aceptar_audio(np.zeros(16000, dtype=np.float32))
        st.procesar()
    check("buffer recortado en sesiones largas", st.buffer.size < 4 * 16000,
          f"tamaño={st.buffer.size}")


# ---------------------------------------------------------------- Inyector: nueva_linea

def test_inyector_nueva_linea():
    """Regresión: 'nuevo párrafo' no debía tipear el carácter '\\n' (xdotool lo
    descarta en silencio); debe enviar pulsaciones reales de Return."""
    from parlar.inyector_salida import Inyector

    for backend in ("xdotool", "wtype", "ydotool"):
        iny = Inyector(backend, notify=False)
        llamadas = []
        iny._correr = staticmethod(lambda cmd, _c=llamadas: (_c.append(cmd), True)[1])

        ok = iny.nueva_linea(2)
        check(f"{backend}: nueva_linea(2) reporta éxito", ok is True)

        comandos = " ".join(" ".join(c) for c in llamadas)
        check(f"{backend}: no se tipea '\\n' como texto",
              "type" not in comandos or "\\n" not in comandos, comandos)
        if backend == "xdotool":
            check("xdotool: usa key Return con --repeat 2",
                  "Return" in comandos and "--repeat" in comandos and "2" in comandos,
                  comandos)
        elif backend == "wtype":
            check("wtype: dos '-k Return'", comandos.count("Return") == 2, comandos)
        elif backend == "ydotool":
            check("ydotool: dos pares de keycode 28 (Enter)",
                  comandos.count("28:1") == 2 and comandos.count("28:0") == 2, comandos)


# ---------------------------------------------------------------- SalidaSesion

def test_salida_sesion():
    import shutil
    import tempfile
    from pathlib import Path
    from parlar.sesion import SalidaSesion, SesionNula, crear_salida_sesion

    tmp = Path(tempfile.mkdtemp(prefix="parlar-sesion-test-"))
    try:
        sesion = SalidaSesion(directorio=tmp)
        check("crea el directorio de sesiones", tmp.is_dir())
        check("nombre de archivo con patrón AAAA-MM-DD_HHMM.txt",
              sesion.ruta.name.endswith(".txt") and len(sesion.ruta.stem) == 15,
              sesion.ruta.name)

        ok1 = sesion.escribir_texto("hola mundo")
        ok2 = sesion.escribir_texto("segunda línea")
        check("escribir_texto reporta éxito", ok1 and ok2)

        contenido = sesion.ruta.read_text(encoding="utf-8")
        check("ambas líneas quedan en el archivo",
              contenido == "hola mundo\nsegunda línea\n", repr(contenido))

        check("texto vacío no escribe nada", sesion.escribir_texto("") is False)
        sesion.evento_vad(True)  # no debe lanzar ni afectar el archivo

        sesion.cerrar()
        check("cerrar no lanza si se llama dos veces",
              sesion.cerrar() is None)

        nula = crear_salida_sesion(False)
        check("crear_salida_sesion(False) devuelve SesionNula",
              isinstance(nula, SesionNula))
        check("SesionNula.escribir_texto no crea archivos",
              nula.escribir_texto("no debería persistir") is False)
        check("directorio de tmp no tiene archivos extra de la nula",
              len(list(tmp.iterdir())) == 1)  # solo el de 'sesion'

        activa = crear_salida_sesion(True, directorio=tmp)
        check("crear_salida_sesion(True) devuelve SalidaSesion",
              isinstance(activa, SalidaSesion))
        activa.cerrar()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_segmentador()
    test_procesador_texto()
    test_espanol()
    test_confirmacion_streaming()
    test_recorte_streaming()
    test_inyector_nueva_linea()
    test_salida_sesion()
    print()
    if FALLAS:
        print(f"{len(FALLAS)} FALLARON: {FALLAS}")
        sys.exit(1)
    print("Todos los tests pasaron.")
