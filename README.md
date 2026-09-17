# GatoFlow 🐈

Un gato bailando siempre visible encima de todo, con temporizador Pomodoro integrado.
El gato aparece mientras trabajas y se esconde en los descansos — para mantener el
flow state sin mirar el reloj.

## Cómo usarla

1. Descarga `GatoFlow.exe` desde la sección **Releases** y guárdalo donde
   quieras (Descargas, Escritorio...). No se instala nada: todo va compilado
   dentro del propio `.exe`, así que funciona con solo abrirlo.
2. Aparece el gato bailando en una ventanita flotante (siempre encima de todo).
3. Pasa el mouse por encima para ver los controles:
   - **▶ / ⏸**: iniciar o pausar el temporizador.
   - **⏭**: saltar a la siguiente fase (trabajo ↔ descanso).
   - **↺**: reiniciar el día.
   - **⚙**: configuración.
   - **✕**: cerrar.
4. Puedes **arrastrar** la ventana a donde quieras y **cambiar su tamaño** desde
   cualquier borde o esquina.
5. Debajo del video siempre ves el tiempo restante.

Solo puedes tener una abierta a la vez: si intentas abrirla dos veces, te avisa
y no duplica el gato.

## Cómo funciona el Pomodoro

- Trabajas por sesiones (por defecto 25 minutos) y descansas entre ellas
  (5 minutos, o 15 cada 4 sesiones).
- **Mientras trabajas**: sale el gato bailando + el temporizador.
- **En descanso**: el gato se esconde y queda solo el temporizador.
- Al completar tu meta del día, te felicita. 🎉

Todo se configura con el botón **⚙**: duración del trabajo y descansos
(el descanso puede ser 0 para saltarlo), cantidad de sesiones, meta del día,
sonido y auto-inicio. Tus ajustes se guardan solos en `%APPDATA%\GatoFlow`
(el lugar estándar de Windows para esto): junto al `.exe` no se crea nada.

## Velocidad del video

- **Manual**: eliges un porcentaje fijo (50–200%).
- **AUTO (beta)**: escucha lo que suena y sigue el tempo (rap lento,
  techno rápido; nunca baja de 100%). El porcentaje se ve junto al
  temporizador (ej. `· 150%`). Un solo aviso sonoro a la vez.

## En Mac

También hay versión para Mac (se compila desde este mismo código):
descarga `GatoFlow-macOS-arm64.zip` desde **Releases**, descomprímelo y
arrastra `GatoFlow.app` a Aplicaciones. Funciona igual que en Windows,
con dos detalles:

- El modo de velocidad **AUTO necesita [BlackHole](https://existential.audio/blackhole/)
  (gratuito)**: es el que permite escuchar lo que suena en el Mac, que no
  trae esa opción de fábrica. Sin BlackHole, usa velocidad manual.
- La primera vez macOS pide **permiso de micrófono**: acéptalo, es como el
  sistema llama al permiso para escuchar el audio (no se graba nada).
- Tus ajustes se guardan en `~/Library/Application Support/GatoFlow`.
- **Guía paso a paso para probar en Mac**: ver `MAC.md`.
- El `.app` se compila solo con GitHub Actions (`.github/workflows/`):
  no se puede compilar para Mac desde Windows.

## Para desarrolladores

```bat
pip install -r requirements.txt
run.bat
```

Para generar el `.exe` (Windows + Python 3.11):

```powershell
.\build_exe.ps1
```

Para generar la `.app` (**en un Mac** con Python 3.11):

```bash
bash build_mac.sh
```

> Nota: el `.exe` no se sube al repositorio porque supera el límite de tamaño
> de GitHub — se distribuye por **Releases**.

Estructura:

```
GatoFlow/
├── main.py            # widget + pomodoro + velocidad
├── bpm.py             # ritmo del audio del sistema -> velocidad (solo acelera)
├── assets/            # video del gato (mp4), gif de referencia, iconos
├── requirements.txt
├── run.bat / build_exe.ps1 / build_mac.sh
└── config.json        # solo corriendo desde codigo (el .exe usa %APPDATA%)
```
