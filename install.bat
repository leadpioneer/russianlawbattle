@echo off
rem ============================================================
rem  Установщик «Судебного симулятора» (Windows).
rem  Запуск: install.bat          - установить и открыть браузер
rem          install.bat --no-start - только установить/собрать
rem ============================================================
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo ============================================
echo   Судебный симулятор — установка
echo ============================================

rem --- 1/5: Python 3.11+ -----------------------------------------------------
set "PYEXE="
for %%V in (3.14 3.13 3.12 3.11) do (
  if not defined PYEXE (
    py -%%V -c "import sys" >nul 2>&1 && set "PYEXE=py -%%V"
  )
)
if not defined PYEXE (
  python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && set "PYEXE=python"
)
if not defined PYEXE (
  echo [ОШИБКА] Python 3.11+ не найден.
  echo Установите его отсюда: https://www.python.org/downloads/
  echo Затем запустите install.bat снова.
  pause
  exit /b 1
)
echo [1/5] Python найден: !PYEXE!

rem --- 2/5: Node.js (нужен для фронтенда) -------------------------------------
node --version >nul 2>&1
if errorlevel 1 (
  echo [ОШИБКА] Node.js не найден — он нужен для веб-интерфейса.
  echo Установите LTS-версию отсюда: https://nodejs.org/ и запустите install.bat снова.
  pause
  exit /b 1
)
echo [2/5] Node.js найден.

rem --- 3/5: виртуальное окружение и зависимости бэкенда ----------------------
if not exist ".venv\Scripts\python.exe" (
  echo Создаю виртуальное окружение...
  !PYEXE! -m venv .venv
)
echo [3/5] Обновляю pip и ставлю зависимости Python — это займёт несколько минут...
.venv\Scripts\python.exe -m pip install --upgrade pip >nul
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
  echo [ОШИБКА] Установка зависимостей не удалась — см. сообщения выше.
  pause
  exit /b 1
)

rem --- 4/5: первый запуск — ключ API и конфиг --------------------------------
if exist ".env" (
  echo [4/5] .env уже существует — не трогаю. Ключ можно поменять в веб-форме.
) else (
  echo.
  echo [4/5] Первичная настройка. Нажмите Enter, чтобы пропустить и заполнить потом в веб-форме.
  set "ROUTER_URL=https://routerai.ru/api/v1"
  set "API_KEY="
  set /p "ROUTER_URL=API роутера [Enter = https://routerai.ru/api/v1]: "
  set /p "API_KEY=Ключ API (вставьте или Enter — пропустить): "
  > .env echo ROUTERAI_API_KEY=!API_KEY!
  >> .env echo # Полный ключ можно также указать в веб-форме на первом шаге.
  echo Файл .env создан.
)
if exist "config.yaml" (
  echo Конфиг config.yaml уже существует — не трогаю.
) else (
  > config.yaml echo api_base_url: "https://routerai.ru/api/v1"
  >> config.yaml echo api_key_env: "ROUTERAI_API_KEY"
  >> config.yaml echo model_claimant_lawyer: "~z-ai/glm-flash-latest"
  >> config.yaml echo model_defendant_lawyer: "~z-ai/glm-flash-latest"
  >> config.yaml echo model_judge: "~z-ai/glm-flash-latest"
  >> config.yaml echo jurisdiction: "Российская Федерация, гражданское право"
  >> config.yaml echo max_rounds: 3
  >> config.yaml echo max_context_tokens: 950000
  >> config.yaml echo llm_params:
  >> config.yaml echo   reasoning: {"effort": "low"}
  echo Создан config.yaml с настройками по умолчанию — routerai + GLM Flash.
)

rem --- 5/5: фронтенд (Next.js) ------------------------------------------------
if not exist "web\node_modules" (
  echo [5/5] Ставлю зависимости фронтенда — npm install, это займёт несколько минут...
  pushd web
  call npm install
  popd
  if errorlevel 1 (
    echo [ОШИБКА] npm install не удался. Проверьте установку Node.js: https://nodejs.org/
    pause
    exit /b 1
  )
) else (
  echo [5/5] Зависимости фронтенда уже установлены.
)

echo Собираю фронтенд — npm run build...
pushd web
call npm run build
popd
set "FRONT_MODE=start"
if errorlevel 1 (
  echo [ВНИМАНИЕ] Сборка не удалась — фронтенд запустится в режиме разработки.
  set "FRONT_MODE=dev"
)

if "%~1"=="--no-start" (
  echo.
  echo Готово. Запуск вручную:
  echo   backend:  .venv\Scripts\python.exe -m uvicorn src.api:app --port 8000
  echo   frontend: cd web, затем npm run start
  endlocal
  exit /b 0
)

rem --- запуск ----------------------------------------------------------------
echo.
echo Запускаю бэкенд и фронтенд...
start "court-sim backend" cmd /k ".venv\Scripts\python.exe -X utf8 -m uvicorn src.api:app --host 127.0.0.1 --port 8000"
start "court-sim frontend" cmd /k "cd web && npm run !FRONT_MODE! -- -p 3000"
echo Жду готовности серверов...
timeout /t 8 /nobreak >nul
start http://localhost:3000

echo.
echo ============================================
echo   Приложение открыто в браузере.
echo   Закройте окна "court-sim ..." для остановки.
echo ============================================
endlocal
