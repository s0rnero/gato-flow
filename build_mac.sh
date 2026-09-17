#!/bin/bash
# GatoFlow: compila el .app de macOS (ejecutar EN un Mac con Python 3.11). Deja
# dist/GatoFlow.app listo para arrastrar a Aplicaciones: bundle --onedir (--onefile
# desempaquetaria ~300 MB en cada arranque y duplicaria procesos), icono .icns
# generado desde icon.png, compatibilidad con macOS 13+ via MACOSX_DEPLOYMENT_TARGET,
# texto de permiso de microfono (exigido para capturar audio, incluido BlackHole),
# firma ad-hoc (evita el dialogo de "app danada"; igual pide clic derecho -> Abrir
# la primera vez) y smoke test offscreen (viva 15 s = OK; con sleep+kill porque
# `timeout` de GNU no existe en macOS). Limpia artefactos al terminar.
set -e
cd "$(dirname "$0")"

pip install -r requirements.txt pyinstaller

rm -rf assets/GatoFlow.iconset
mkdir -p assets/GatoFlow.iconset
for s in 16 32 128 256 512; do
  sips -z $s $s assets/icon.png --out "assets/GatoFlow.iconset/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) assets/icon.png --out "assets/GatoFlow.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns assets/GatoFlow.iconset -o assets/icon.icns 2>/dev/null || \
  python -c "from PIL import Image; Image.open('assets/icon.png').save('assets/icon.icns')"
rm -rf assets/GatoFlow.iconset

export MACOSX_DEPLOYMENT_TARGET="13.0"
pyinstaller --noconfirm --clean --windowed --onedir \
  --name GatoFlow \
  --icon "assets/icon.icns" \
  --osx-bundle-identifier com.gatoflow.app \
  --collect-binaries cv2 \
  --collect-all soundcard \
  --add-data "assets:assets" \
  main.py

MSG="GatoFlow escucha la música que suena para acelerar al gato con el ritmo."
plutil -replace NSMicrophoneUsageDescription -string "$MSG" dist/GatoFlow.app/Contents/Info.plist 2>/dev/null || \
/usr/libexec/PlistBuddy -c "Add :NSMicrophoneUsageDescription string '$MSG'" dist/GatoFlow.app/Contents/Info.plist 2>/dev/null || \
echo "AVISO: agrega NSMicrophoneUsageDescription al Info.plist a mano."

codesign --force --deep -s - dist/GatoFlow.app
codesign --verify --deep --strict dist/GatoFlow.app

QT_QPA_PLATFORM=offscreen ./dist/GatoFlow.app/Contents/MacOS/GatoFlow & APP_PID=$!
sleep 15
if kill -0 $APP_PID 2>/dev/null; then
  echo "SMOKE OK (app viva 15 s)"; kill -9 $APP_PID
else
  wait $APP_PID; echo "SMOKE FAIL (exit $?)"; exit 1
fi

rm -rf build __pycache__ GatoFlow.spec
echo "Listo: dist/GatoFlow.app (arrástralo a Aplicaciones)"
