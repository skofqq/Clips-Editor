; Inno Setup script for Twitch Shorts Studio. build_installer.bat passes AppVersion.
; Installs for the current user (no administrator needed); settings, models and
; history stay in %LOCALAPPDATA%\TwitchShortsStudio and survive an update or uninstall.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "Twitch Shorts Studio"
#define AppExe "TwitchShortsStudio.exe"
; Models downloaded by tools\fetch_models.py; each folder found becomes a component.
#define Models SourcePath + "..\build\models\"
#define ModelsDest "{localappdata}\TwitchShortsStudio\models"

[Setup]
AppId={{6F1C2B7E-5D3A-4C9E-9B21-7A4E8F2D6C10}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=skof
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\build\installer
OutputBaseFilename=TwitchShortsStudio-Setup-{#AppVersion}
SetupIconFile=app.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/max
SolidCompression=no
; Models make the setup bigger than one file can be: the rest goes to
; TwitchShortsStudio-Setup-<version>-1.bin, -2.bin ... next to the .exe.
DiskSpanning=yes
DiskSliceSize=max
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Types]
Name: "optimal"; Description: "{cm:TypeOptimal}"
Name: "full"; Description: "{cm:TypeFull}"
Name: "minimal"; Description: "{cm:TypeMinimal}"
Name: "custom"; Description: "{cm:TypeCustom}"; Flags: iscustom

[Components]
Name: "program"; Description: "{cm:CompProgram}"; Types: optimal full minimal custom; Flags: fixed
#if DirExists(Models + "speech_large_v3_russian_int8")
Name: "speech_ru"; Description: "{cm:CompSpeechRu}"; Types: optimal full
#endif
#if DirExists(Models + "speech_small")
Name: "speech_small"; Description: "{cm:CompSpeechSmall}"; Types: full
#endif
#if DirExists(Models + "speech_large_v3_turbo")
Name: "speech_turbo"; Description: "{cm:CompSpeechTurbo}"; Types: full
#endif
#if DirExists(Models + "speech_large_v3_russian")
Name: "speech_ru_full"; Description: "{cm:CompSpeechRuFull}"; Types: full
#endif
#if DirExists(Models + "speech_gigaam_v3")
Name: "speech_gigaam"; Description: "{cm:CompSpeechGigaam}"; Types: optimal full
#endif
#if DirExists(Models + "speech_parakeet_v3")
Name: "speech_parakeet"; Description: "{cm:CompSpeechParakeet}"; Types: full
#endif
#if DirExists(Models + "separation")
Name: "separation"; Description: "{cm:CompSeparation}"; Types: optimal full
#endif
#if DirExists(Models + "speakers")
Name: "speakers"; Description: "{cm:CompSpeakers}"; Types: optimal full
#endif
#if DirExists(Models + "face")
Name: "face"; Description: "{cm:CompFace}"; Types: optimal full
#endif

[CustomMessages]
ru.TypeOptimal=Оптимальный: программа, русские модели речи (Whisper и GigaAM) и мелкие модели
ru.TypeFull=Полный: программа и все модели из установщика
ru.TypeMinimal=Минимальный: только программа (модели можно скачать в ней)
ru.TypeCustom=Выборочный
ru.CompProgram=Программа
ru.CompSpeechRu=Распознавание речи: русская модель (large-v3-russian-int8, ~1,5 ГБ)
ru.CompSpeechSmall=Распознавание речи: быстрая модель small (~0,5 ГБ, для процессора)
ru.CompSpeechTurbo=Распознавание речи: large-v3-turbo (~1,6 ГБ, для видеокарты)
ru.CompSpeechRuFull=Распознавание речи: large-v3-russian (~3 ГБ, для видеокарты)
ru.CompSpeechGigaam=Распознавание речи: GigaAM v3, русский, точнее ловит мат (~250 МБ)
ru.CompSpeechParakeet=Распознавание речи: Parakeet v3, лучше для английского (~670 МБ)
ru.CompSeparation=Отделение голоса от музыки (64 МБ)
ru.CompSpeakers=Определение говорящих (28 МБ)
ru.CompFace=Поиск лиц для слежения за вебкой (2 МБ)
en.TypeOptimal=Optimal: the program, the Russian speech models (Whisper and GigaAM) and the small models
en.TypeFull=Full: the program and every model in the setup
en.TypeMinimal=Minimal: the program only (models can be downloaded in it)
en.TypeCustom=Custom
en.CompProgram=Program
en.CompSpeechRu=Speech recognition: Russian model (large-v3-russian-int8, ~1.5 GB)
en.CompSpeechSmall=Speech recognition: fast small model (~0.5 GB, for the CPU)
en.CompSpeechTurbo=Speech recognition: large-v3-turbo (~1.6 GB, for a GPU)
en.CompSpeechRuFull=Speech recognition: large-v3-russian (~3 GB, for a GPU)
en.CompSpeechGigaam=Speech recognition: GigaAM v3, Russian, catches swearing better (~250 MB)
en.CompSpeechParakeet=Speech recognition: Parakeet v3, best for English (~670 MB)
en.CompSeparation=Separating the voice from music (64 MB)
en.CompSpeakers=Telling speakers apart (28 MB)
en.CompFace=Finding faces to follow the webcam (2 MB)

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\build\dist\TwitchShortsStudio\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: program
; Models go where the app keeps them; already there = kept; uninstall leaves them.
#define ModelFlags "onlyifdoesntexist recursesubdirs createallsubdirs uninsneveruninstall nocompression"
#if DirExists(Models + "speech_large_v3_russian_int8")
Source: "{#Models}speech_large_v3_russian_int8\*"; DestDir: "{#ModelsDest}"; Flags: {#ModelFlags}; Components: speech_ru
#endif
#if DirExists(Models + "speech_small")
Source: "{#Models}speech_small\*"; DestDir: "{#ModelsDest}"; Flags: {#ModelFlags}; Components: speech_small
#endif
#if DirExists(Models + "speech_large_v3_turbo")
Source: "{#Models}speech_large_v3_turbo\*"; DestDir: "{#ModelsDest}"; Flags: {#ModelFlags}; Components: speech_turbo
#endif
#if DirExists(Models + "speech_large_v3_russian")
Source: "{#Models}speech_large_v3_russian\*"; DestDir: "{#ModelsDest}"; Flags: {#ModelFlags}; Components: speech_ru_full
#endif
#if DirExists(Models + "speech_gigaam_v3")
Source: "{#Models}speech_gigaam_v3\*"; DestDir: "{#ModelsDest}"; Flags: {#ModelFlags}; Components: speech_gigaam
#endif
#if DirExists(Models + "speech_parakeet_v3")
Source: "{#Models}speech_parakeet_v3\*"; DestDir: "{#ModelsDest}"; Flags: {#ModelFlags}; Components: speech_parakeet
#endif
#if DirExists(Models + "separation")
Source: "{#Models}separation\*"; DestDir: "{#ModelsDest}\separation"; Flags: {#ModelFlags}; Components: separation
#endif
#if DirExists(Models + "speakers")
Source: "{#Models}speakers\*"; DestDir: "{#ModelsDest}\speakers"; Flags: {#ModelFlags}; Components: speakers
#endif
#if DirExists(Models + "face")
Source: "{#Models}face\*"; DestDir: "{#ModelsDest}\face"; Flags: {#ModelFlags}; Components: face
#endif

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
