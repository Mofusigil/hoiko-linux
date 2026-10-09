#include <SDL2/SDL.h>
#include <iostream>
int main() {
    if(SDL_Init(SDL_INIT_GAMECONTROLLER)!=0) {std::cerr<<SDL_GetError()<<'\n';return 1;}
    for(int i=0;i<SDL_NumJoysticks();++i) {
        char guid[33];SDL_JoystickGetGUIDString(SDL_JoystickGetDeviceGUID(i),guid,sizeof(guid));
        std::cout<<i<<" "<<SDL_JoystickNameForIndex(i)<<" guid="<<guid
                 <<" gamecontroller="<<SDL_IsGameController(i)<<'\n';
    }
    SDL_Quit();
}
