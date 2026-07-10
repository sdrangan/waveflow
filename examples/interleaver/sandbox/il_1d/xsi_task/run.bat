@echo off
cd /d "%~dp0"
set VIV=C:\Xilinx\2025.1\Vivado
set MINGW=%VIV%\tps\mingw\6.2.0\win64.o\nt
set PATH=%~dp0xsim.dir\interleaver;%MINGW%\bin;%VIV%\lib\win64.o;%VIV%\bin;%PATH%
echo --- xvlog RTL ---
call %VIV%\bin\xvlog -f rtl.f
echo xvlog errorlevel=%ERRORLEVEL%
echo --- xelab -dll ---
call %VIV%\bin\xelab work.interleaver -dll -s interleaver -debug typical
echo xelab errorlevel=%ERRORLEVEL%
echo --- g++ BFM tb ---
call %MINGW%\bin\g++.exe -I%VIV%\data\xsim\include -O3 -c -o xsi_loader.o xsi_loader.cpp
call %MINGW%\bin\g++.exe -I%VIV%\data\xsim\include -O3 -c -o il_bfm_tb.o il_bfm_tb.cpp
call %MINGW%\bin\g++.exe -o il_bfm.exe il_bfm_tb.o xsi_loader.o
echo gpp errorlevel=%ERRORLEVEL%
echo --- run ---
.\il_bfm.exe
echo XSI_EXITCODE=%ERRORLEVEL%
