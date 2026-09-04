@echo off
rem run.bat <top> <tb_basename> [trace] — XSI flow (xvlog -> xelab -dll -> g++ BFM -> run) for a
rem generated free-running mem-stream kernel.  Adapted from the interleaver sandbox xsi_task/run.bat.
rem   run.bat mem_r_stream mem_r_bfm_tb
rem   run.bat mem_w_stream mem_w_bfm_tb
rem
rem Pass a third argument `trace` to also elaborate vcd_dumper_<top>.v as a SECOND top, whose
rem $dumpvars writes <top>_trace.vcd.  The XSI top -- and so every BFM port number -- is untouched,
rem so the cycle counts are identical either way; only the dump is added.  The dumper is per-top
rem because one xsi/ directory can serve several (examples/interleaver/xsi builds three), and a
rem dumper naming a scope that is not part of THIS elaboration is a hard error.
rem   run.bat mem_copy mem_copy_bfm_tb trace
rem
rem Note: re-running the built .exe does NOT regenerate the VCD -- only this script does, because
rem the dump comes from the elaborated snapshot.  See waveflow.build.trace_steps.RtlSimStep.
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
