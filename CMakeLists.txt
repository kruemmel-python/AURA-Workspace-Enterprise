cmake_minimum_required(VERSION 3.24)
project(AuraWorkspaceEnterprise VERSION 2.4.4 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_executable(aura_workspace WIN32 aura_workspace.cpp core/enterprise_database.cpp)
target_include_directories(aura_workspace PRIVATE "H:/KR-Compiler/KR-Compiler-Windows-x64-v4.3.1/toolchain/include")
target_link_directories(aura_workspace PRIVATE "H:/KR-Compiler/KR-Compiler-Windows-x64-v4.3.1/toolchain/lib")
target_link_libraries(aura_workspace PRIVATE sqlite3 user32 gdi32 comdlg32)
