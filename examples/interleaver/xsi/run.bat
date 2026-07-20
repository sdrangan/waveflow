@echo off
rem run.bat <top> <tb_basename> [trace] — XSI flow (xvlog -> xelab -dll -> g++ BFM -> run) for a
rem generated free-running mem-stream kernel.  Adapted from the interleaver sandbox xsi_task/run.bat.
rem   run.bat mem_r_stream mem_r_bfm_tb
rem   run.bat mem_w_stream mem_w_bfm_tb
rem
rem Pass a third argument `trace` to also elaborate vcd_dumper_<top>.v as a SECOND top, whose
rem $dumpvars writes <top>_trace.vcd.  That leaves the XSI top -- and every BFM port number --
rem untouched, so the run is identical apart from the dump; the gate cycle counts are unchanged.
rem The dumper is per-top because this directory serves three of them.
rem   run.bat interleaver_canon interleaver_canon_bfm_tb trace
cd /d "%~dp0"
set VIV=C:\Xilinx\2025.1\Vivado
set MINGW=%VIV%\tps\mingw\6.2.0\win64.o\nt
set TOP=%1
set TB=%2
set PATH=%~dp0xsim.dir\%TOP%;%MINGW%\bin;%VIV%\lib\win64.o;%VIV%\bin;%PATH%
echo --- xvlog RTL (%TOP%) ---
call %VIV%\bin\xvlog -f rtl_%TOP%.f
echo xvlog errorlevel=%ERRORLEVEL%
rem The two xelab lines are spelled out rather than built in a variable: setting one inside an
rem if/else block needs delayed expansion, which is a classic cmd trap.
if /I "%3"=="trace" (
  echo --- xvlog vcd_dumper_%TOP% ---
  call %VIV%\bin\xvlog vcd_dumper_%TOP%.v
  echo --- xelab -dll [+ vcd_dumper_%TOP%] ---
  call %VIV%\bin\xelab work.%TOP% work.vcd_dumper_%TOP% -dll -s %TOP% -debug typical
) else (
  echo --- xelab -dll ---
  call %VIV%\bin\xelab work.%TOP% -dll -s %TOP% -debug typical
)
echo xelab errorlevel=%ERRORLEVEL%
echo --- g++ BFM tb (%TB%) ---
call %MINGW%\bin\g++.exe -I%VIV%\data\xsim\include -O3 -c -o xsi_loader.o xsi_loader.cpp
call %MINGW%\bin\g++.exe -I%VIV%\data\xsim\include -O3 -c -o %TB%.o %TB%.cpp
call %MINGW%\bin\g++.exe -o %TB%.exe %TB%.o xsi_loader.o
echo gpp errorlevel=%ERRORLEVEL%
echo --- run ---
.\%TB%.exe
echo XSI_EXITCODE=%ERRORLEVEL%
