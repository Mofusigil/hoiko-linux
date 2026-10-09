#include <array>
#include <cstdlib>
#include <fstream>
#include <mutex>
#include <sstream>
#include <string>
#include <vector>
#include "generated/core.inc"
#include "generated/bindings.inc"

namespace {
std::mutex engine_mutex;
std::string error_message;
std::string weight_directory;
std::vector<unsigned char> tree(56580000+64), babies(36000+64),
    expanded_babies(8064), expanded_children(6720), evaluator(40), weights(1088);
alignas(8) std::array<unsigned char,968> root{};
bool initialized=false, have_root=false;
int configured_depth=10, configured_beam=250;
double search_budget=112;
template<class F> Slot invoke(F f,std::initializer_list<Slot> a={}) { return f(a.begin()); }
void put32(void* p,int32_t v) { store(p,Slot::i32(v),4); }
void put64(void* p,void* v) { store(p,Slot::pointer(v),8); }
void putflag(void* p,bool v) { store(p,Slot::i32(v),1); }
void read_weights(unsigned char* target,const std::string& path) {
    std::ifstream file(path);
    if(!file) throw std::runtime_error("Cannot read weight file: "+path);
    std::string line; int row=0;
    while(std::getline(file,line)) {
        if(!line.empty() && line.back()=='\r') line.pop_back();
        std::istringstream record(line); std::string label,value;
        std::getline(record,label,','); std::vector<int> values;
        while(std::getline(record,value,',')) values.push_back(std::stoi(value));
        if(row==13) {
            if(values.size()!=10) throw std::runtime_error("Invalid column weights");
            for(int j=0;j<10;++j) put32(target+200+j*4,values[j]);
        } else if(row==44) {
            if(values.empty()) throw std::runtime_error("Invalid combo weights");
            for(int j=0;j<5;++j) put32(target+240+j*4,
                values.size()==5 ? values[j] : values[0]*(j+1));
        } else {
            if(values.size()!=1 || row>46) throw std::runtime_error("Invalid weight row");
            put32(target+row*4,values[0]);
        }
        ++row;
    }
    if(row!=47) throw std::runtime_error("Expected 47 weight rows in "+path);
    store(target+264,Slot::real(std::clamp(read_value<int32_t>(target+184),0,100)*0.01),8);
}
struct NodeStorage { unsigned char data[328]; };
static_assert(sizeof(NodeStorage)==328);
}

Slot native_engine_init(const Slot*) {
    put64(G_SearchAI_tree,tree.data()+32);
    put64(G_SearchAI_multiEvaluator,evaluator.data());
    put64(G_ExpandAI_babyNode,babies.data()+32);
    put64(G_ExpandAI_expandedBaby,expanded_babies.data());
    put64(G_ExpandAI_expandedChild,expanded_children.data());
    put64(G_EvaluateAI_weightList,weights.data());
    put64(G_EvaluateAI_weight,weights.data());
    return {};
}
Slot native_weights_ready(const Slot*) {
    const int style=read_value<int32_t>(G_ConfigAI_playStyle);
    const char* files[4]={"we.dll","wo.dll","wd.dll","wr.dll"};
    if(style==1) files[0]=files[1]="sprint.dll";
    if(style==2) {files[0]="ultra1.dll";files[1]="ultra2.dll";}
    for(int i=0;i<4;++i) read_weights(weights.data()+i*272,weight_directory+"/"+files[i]);
    put32(G_EvaluateAI_playStyle,style);
    put64(G_EvaluateAI_weight,weights.data());
    return {};
}
Slot native_sequential_worker(const Slot* a) {
    std::memset(a[0].ptr(),0,40); return {};
}
Slot native_partial_sort(const Slot* a) {
    auto* first=static_cast<NodeStorage*>(a[0].ptr());
    auto* mid=static_cast<NodeStorage*>(a[1].ptr());
    auto* last=static_cast<NodeStorage*>(a[2].ptr());
    std::partial_sort(first,mid,last,[](const auto& x,const auto& y) {
        return read_value<int32_t>(x.data+308)>read_value<int32_t>(y.data+308);
    });
    return {};
}
static Slot partition_bytes(const Slot* a,int stride,int offset,int threshold,bool less) {
    auto* first=static_cast<unsigned char*>(a[0].ptr());
    auto* last=static_cast<unsigned char*>(a[1].ptr());
    std::array<unsigned char,328> temp;
    auto pass=[&](const unsigned char* p) {
        int v=read_value<int32_t>(p+offset);return less?v<threshold:v>threshold;
    };
    while(first!=last) {
        if(pass(first)) {first+=stride;continue;}
        do {last-=stride;if(first==last)return Slot::pointer(first);} while(!pass(last));
        std::memcpy(temp.data(),first,stride);std::memcpy(first,last,stride);
        std::memcpy(last,temp.data(),stride);first+=stride;
    }
    return Slot::pointer(first);
}
Slot native_partition_baby(const Slot* a) { return partition_bytes(a,120,80,10000,true); }
Slot native_partition_node(const Slot* a) { return partition_bytes(a,328,308,-1000000,false); }

extern "C" {
struct HoikoResult {
    char commands[48];
    int32_t x,y,rotation,piece,depth,nodes,beam,clear_action;
    int32_t board[32];
    double elapsed_ms;
};
const char* hoiko_error() { return error_message.c_str(); }
int hoiko_init(const char* directory,int game,int beam,int depth,double budget,int style,int hold,int td) {
    std::lock_guard<std::mutex> lock(engine_mutex);
    try {
        if(!directory || game<1 || game>2 || beam<1 || beam>500 || depth<1 || depth>14 ||
           budget<=0 || budget>10000 || style<0 || style>2) throw std::runtime_error("Invalid engine options");
        weight_directory=directory;configured_depth=depth;configured_beam=beam;search_budget=budget;
        invoke(R_SearchAI_Init_0);
        putflag(G_ConfigAI_useS4W,false);putflag(G_ConfigAI_loopTemplate,false);
        put32(G_ConfigAI_viewNext,5);putflag(G_ConfigAI_useHold,hold);
        put32(G_ConfigAI_beamSize,beam);put32(G_ConfigAI_threadCount,1);
        put32(G_ConfigAI_minDepth,depth);put32(G_ConfigAI_speed,100);
        put32(G_ConfigAI_playStyle,style);putflag(G_ConfigAI_useTDOpeners,td);
        store(G_ConfigAI_takeOverTime,Slot::real(5),8);
        put32(G_HoldMino_ppt,game);
        invoke(R_SearchAI_ReadConfig_0);
        store(G_EvaluateAI_trustRate,Slot::real(1),8);
        initialized=true;have_root=false;error_message.clear();return 0;
    }catch(const std::exception& e){error_message=e.what();initialized=false;return -1;}
}
int hoiko_options(int s4w,int loop) {
    std::lock_guard<std::mutex> lock(engine_mutex);
    try {
        if(!initialized) throw std::runtime_error("Engine is not initialized");
        putflag(G_ConfigAI_useS4W,s4w);
        putflag(G_ConfigAI_loopTemplate,loop);
        invoke(R_SearchAI_ReadConfig_0);
        have_root=false; return 0;
    }catch(const std::exception& e){error_message=e.what();return -1;}
}
int hoiko_solve(const int32_t* board,const int32_t* pieces,int count,int hold,int combo,int btb,int reset,HoikoResult* out) {
    std::lock_guard<std::mutex> lock(engine_mutex);
    try {
        if(!initialized || !board || !pieces || !out || count<2 || count>12 || hold<0 || hold>7)
            throw std::runtime_error("Invalid state or uninitialized engine");
        for(int i=0;i<32;++i) if(board[i]<0 || board[i]>1023)throw std::runtime_error("Invalid board row");
        for(int i=0;i<count;++i) if(pieces[i]<0 || pieces[i]>6)throw std::runtime_error("Invalid piece");
        auto started=std::chrono::steady_clock::now();
        put32(G_ConfigAI_viewNext,count);put32(G_NextMino_viewNext,count);
        alignas(8) unsigned char mimic[272]{};
        std::memcpy(mimic,board,128);
        invoke(R_Board_UpdateMaxHeight_1,{Slot::pointer(mimic)});
        put32(mimic+136,hold);
        for(int i=0;i<count;++i) put32(mimic+140+i*4,pieces[i]);
        put32(mimic+260,combo);put32(mimic+264,btb);
        if(reset || !have_root) {
            root.fill(0);
            invoke(R_SearchAI_ReadConfig_0);
            invoke(R_RootNode_Init_2,{Slot::pointer(root.data()),Slot::pointer(pieces)});
            std::memcpy(root.data()+216,mimic,136);
            put32(root.data()+136,hold);put32(root.data()+408,combo);
            put32(root.data()+412,btb?1:0);
            invoke(R_RootNode_SetNewCurrent_1,{Slot::pointer(root.data())});
        } else if(invoke(R_RootNode_IsSameAs_2,{Slot::pointer(root.data()),Slot::pointer(mimic)}).truth()) {
            invoke(R_RootNode_SetPPTData_2,{Slot::pointer(root.data()),Slot::pointer(mimic)});
        } else {
            invoke(R_RootNode_ResetPPTData_2,{Slot::pointer(root.data()),Slot::pointer(mimic)});
        }
        // The portable API receives current + visible next, whereas the original
        // front end maintains a second speculative root between turns. Reconcile
        // the authoritative visible prefix and hold before searching this root.
        std::memcpy(root.data(),pieces,count*4);
        std::memcpy(root.data()+216,mimic,136);
        put32(root.data()+136,hold);put32(root.data()+140,0);
        put32(root.data()+408,combo);
        if(!btb) put32(root.data()+412,0);
        else if(read_value<int32_t>(root.data()+412)<1) put32(root.data()+412,1);
        invoke(R_RootNode_SetNewCurrent_1,{Slot::pointer(root.data())});
        have_root=true;
        put32(G_SearchAI_depthLimit,configured_depth);
        put32(G_SearchAI_minDepth,configured_depth);
        put32(G_SearchAI_beamSizeLimit,configured_beam);
        store(G_SearchAI_timer,native_ticks(nullptr),8);
        store(G_SearchAI_timer+8,Slot::real(search_budget),8);
        putflag(G_SearchAI_isSearching,true);
        invoke(R_EvaluateAI_EvenOrOdd_1,{Slot::pointer(root.data())});
        invoke(R_SearchAI_Search_1,{Slot::pointer(root.data())});
        std::memset(out,0,sizeof(*out));
        std::memcpy(out->commands,root.data()+352,47);
        out->x=read_value<int32_t>(root.data()+184);
        out->y=read_value<int32_t>(root.data()+188);
        out->rotation=read_value<int32_t>(root.data()+192);
        out->piece=read_value<int32_t>(root.data()+196);
        out->depth=read_value<int32_t>(root.data()+444);
        out->nodes=read_value<int32_t>(root.data()+440);
        out->beam=read_value<int32_t>(root.data()+452);
        out->clear_action=read_value<int32_t>(root.data()+416);
        std::memcpy(out->board,root.data()+216,128);
        out->elapsed_ms=std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-started).count();
        return 0;
    }catch(const std::exception& e){error_message=e.what();have_root=false;return -1;}
}
}
