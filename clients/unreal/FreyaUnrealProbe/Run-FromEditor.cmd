@echo off
rem Runs the Unreal window from the Editor's own program, without packaging.
rem Freya starts this when FREYA_UNREAL_CLIENT names it; she supplies the port
rem and pairing key in the environment. Needs the Editor target to be built
rem (Build-Probe.ps1 -Target Editor). FREYA_UNREAL_ENGINE names the engine folder.
if "%FREYA_UNREAL_ENGINE%"=="" set "FREYA_UNREAL_ENGINE=D:\UE_5.8"
"%FREYA_UNREAL_ENGINE%\Engine\Binaries\Win64\UnrealEditor.exe" "%~dp0FreyaUnrealProbe.uproject" /Game/Maps/ProbeLevel -game -nosplash %*
