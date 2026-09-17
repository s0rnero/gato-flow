# GatoFlow: compila el .exe de Windows (ejecutar en esta carpeta con Python 3.11,
# requirements y pyinstaller instalados). Genera un unico GatoFlow.exe autocontenido
# en la raiz (--onefile, sin consola) con binarios de OpenCV, libreria de audio y
# assets incluidos; mata cualquier GatoFlow en ejecucion antes de compilar y borra
# artefactos (dist/build/pycache) al terminar.

Get-Process -Name "GatoFlow" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

pyinstaller --noconfirm --clean --noconsole --onefile `
  --name GatoFlow `
  --icon "assets\icon.ico" `
  --collect-binaries cv2 `
  --collect-all soundcard `
  --add-data "assets;assets" `
  main.py

Move-Item -LiteralPath "dist\GatoFlow.exe" -Destination ".\GatoFlow.exe" -Force
Remove-Item -LiteralPath ".\dist" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath ".\build" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath ".\__pycache__" -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "`nListo: .\GatoFlow.exe (unico ejecutable)" -ForegroundColor Green
