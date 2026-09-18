# Probar GatoFlow en Mac 🍎 (guía para testers, sin tecnicismos)

## 1. Instalar BlackHole (solo para el modo AUTO)

El Mac no deja escuchar lo que suena por defecto. BlackHole (gratuito) lo permite:

1. Descárgalo de **https://existential.audio/blackhole/** e instálalo.
2. Abre **Configuración de Audio MIDI** (está en Aplicaciones → Utilidades).
3. Pulsa `+` abajo a la izquierda → **Crear dispositivo de salida múltiple**.
4. Marca **BlackHole** y también tus **altavoces/auriculares** (así oyes la
   música Y la app la puede analizar a la vez).
5. Clic derecho sobre ese dispositivo múltiple → **Usar para la salida de sonido**.

Sin este paso, el modo AUTO no escucha nada (el modo manual funciona igual).

## 2. Instalar GatoFlow

1. Mira qué chip tiene tu Mac: logo  → **Acerca de este Mac** → **Chip**.
   - Dice M1/M2/M3/M4 → descarga `GatoFlow-macOS-arm64.zip`.
   - Dice Intel → descarga `GatoFlow-macOS-x64.zip`.
2. Descomprímelo y arrastra `GatoFlow.app` a **Aplicaciones**.

## 3. Primera apertura (importante)

Como la app no viene de la App Store, macOS la frena la primera vez:

1. **No** la abras con doble click: haz **clic derecho → Abrir → Abrir**.
2. Si aun así la bloquea: Ajustes → Privacidad y seguridad → baja hasta
   "GatoFlow" → **Abrir de todos modos**.
3. Acepta el permiso de **micrófono**: es como macOS llama al permiso para
   escuchar el audio del sistema (la app no graba ni guarda nada).

Solo hay que hacerlo una vez.

## 4. Probar

1. Pon música (Spotify, YouTube, lo que sea).
2. En el gato: pasa el mouse por encima → **⚙** → activa
   **"Velocidad AUTO (beta)"**.
3. En unos 4–5 segundos el gato se engancha al beat y junto al temporizador
   aparecen el tempo detectado y la velocidad (ej. `♪128 · 124%`).
4. Prueba también: arrastrar, cambiar tamaño desde los bordes, pausar,
   saltar fase con ⏭, cerrar con ✕ y volver a abrir (solo permite una).

## Si algo falla

- **No se oye música**: revisa el paso 1 (dispositivo de salida múltiple).
- **AUTO no sigue el ritmo**: confirma BlackHole + permiso de micrófono; el
  modo manual siempre funciona.
- **La app no abre**: repite el paso 3 (clic derecho → Abrir).
