# Cyborg Admin — APK separado

App nativo (Capacitor) que abre **apenas o painel administrativo**
(`https://cyborgai.duckdns.org/chat/admin.html`) numa WebView.
Instala separado do app principal (appId `br.cyborgai.admin`).

## Build 100% por terminal (na sua máquina: Node 18+, JDK 17, Android SDK)

```bash
cd admin-app
npm install
npx cap add android
npx cap sync android
cd android
./gradlew assembleDebug          # no Windows: .\gradlew assembleDebug
```

APK gerado em: `admin-app/android/app/build/outputs/apk/debug/app-debug.apk`

> Se `gradlew` reclamar do SDK, crie `admin-app/android/local.properties`
> com `sdk.dir=/caminho/do/Android/sdk` (ou defina a variável ANDROID_HOME).

## Observações
- Como carrega a URL ao vivo, o app do admin **atualiza sozinho** quando você
  faz deploy no servidor (não precisa recompilar por mudança de visual).
- Recompile só se mudar ícone, nome, splash ou a `server.url`.
- Confirme a URL do painel abrindo `https://cyborgai.duckdns.org/chat/admin.html`
  no navegador. Se o seu nginx servir o admin em outro caminho, ajuste o
  `server.url` em `admin-app/capacitor.config.json`.
